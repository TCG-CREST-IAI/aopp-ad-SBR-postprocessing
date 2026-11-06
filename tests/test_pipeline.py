import http.client
import json
import socket
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from protocol import digest
from stages import parse_key, pairs_for, check_pairs, retain_pairs, validate_ad, check_blocks, retain_blocks
from server import build_server
from tools.create_certs import create
from transport import Client, load_config


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


class StageTests(unittest.TestCase):
    def test_hand_pair_flags_and_first_bit_retention(self):
        pairs=[[0,1],[2,3],[4,5]]
        flags=check_pairs('011100',pairs)
        self.assertEqual(flags,{'P1':0,'P2':1,'P3':1})
        self.assertEqual(retain_pairs('011001',list(range(6)),pairs,flags),('0',[0]))
        for malformed in ([[0,0]],[[0,1],[1,2]],[[0,9]],[[True,1]],[[0]]):
            with self.assertRaises(ValueError):check_pairs('0101',malformed)

    def test_random_pairs_are_disjoint_and_locally_odd(self):
        bits='000000111100'
        pairs=pairs_for(bits)
        self.assertEqual(len(pairs),4)
        self.assertEqual(len({i for p in pairs for i in p}),8)
        self.assertTrue(all(bits[a]!=bits[b] for a,b in pairs))
        self.assertEqual(pairs_for('000'),[])

    def test_variable_ad_equal_opposite_mixed(self):
        for size in (2,3,4,5,8,16):
            bits='0'*(size*3)
            flags=check_blocks(bits,size,['0'*size,'1'*size,'0'*(size-1)+'1'])
            self.assertEqual(flags,[0,0,1])
            self.assertEqual(retain_blocks(bits,list(range(len(bits))),size,flags),('00',[0,size]))
        for length,size in ((0,2),(5,2),(3,4),(4,1),(4,True),(37,2)):
            with self.assertRaises(ValueError):validate_ad(length,size)
        self.assertEqual(parse_key('\ufeff01 10\n'),'0110')
        for bad in ('','01,10','label01'):
            with self.assertRaises(ValueError):parse_key(bad)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        create(self.root/'private',alice_port=free_port(),bob_port=free_port())
        self.config={r:load_config(self.root/'private'/r/'config.json') for r in ('alice','bob','operator')}
        for role in ('alice','bob'):
            self.config[role]['host']='127.0.0.1'
            self.config[role]['input_file']=str(self.root/'private'/role/'DATA.txt')
            self.config[role]['state_dir']=str(self.root/'private'/role/'state')
            Path(self.config[role]['input_file']).write_text('01'*129)
        self.servers={r:build_server(r,self.config[r]) for r in ('alice','bob')}
        self.threads=[]
        for server in self.servers.values():
            t=threading.Thread(target=server.serve_forever,daemon=True);t.start();self.threads.append(t)
        c=self.config['operator']
        self.clients={r:Client(c[r+'_url'],c['ca'],c['cert'],c['key'],c[r+'_cert']) for r in ('alice','bob')}

    def tearDown(self):
        for server in self.servers.values():server.shutdown();server.server_close()
        for t in self.threads:t.join(5)
        self.temp.cleanup()

    def call(self,role,action,run='case',**kw):
        return self.clients[role].post('/pipeline',dict(action=action,run_id=run,**kw))

    def prepare(self,run='case'):
        for role in ('alice','bob'):self.call(role,'prepare',run,input_kind='synthetic')

    def test_full_pipeline_divisibility_reselection_and_restart(self):
        self.prepare()
        pairs=[[i,i+1] for i in range(0,258,2)]
        with patch('pipeline_service.pairs_for',return_value=pairs):
            first=self.call('alice','aopp')
        self.assertEqual(first['current_length'],129)
        self.assertEqual(first['summaries'][0]['retained_positions_digest'],digest(list(range(0,258,2))))
        for role in ('alice','bob'):
            with self.assertRaisesRegex(ValueError,'AOPP checkpoint is unchanged'):
                self.call(role,'select_ad',b=2)
            self.assertEqual(self.call(role,'status')['phase'],'AOPP')
            self.call(role,'select_ad',b=3)
        # Reconstruct checkpoint service as a process restart would, without a key reread.
        from pipeline_service import Pipeline
        self.servers['alice'].service.pipeline=Pipeline(self.servers['alice'].service)
        second=self.call('alice','ad')
        self.assertEqual(second['current_length'],43)
        final=self.call('alice','sbi')
        self.assertEqual(final['phase'],'VERIFIED')
        self.assertEqual(final['current_length'],43)
        outputs=[(Path(self.config[r]['state_dir'])/'case.bits').read_text() for r in ('alice','bob')]
        self.assertEqual(outputs,['0'*43,'0'*43])
        for role in ('alice','bob'):
            private=self.servers[role].service.pipeline.load('case')
            self.assertNotIn('proposal',private)
            self.assertNotIn('draft',private)
            self.assertNotIn('sbi_attempt',private)
            self.assertEqual(private['bits'],'')
            self.assertEqual(private['positions'],[])
        self.assertEqual(self.call('alice','sbi')['phase'],'VERIFIED')

    def test_dropped_commit_response_retries_without_rerunning_aopp(self):
        self.prepare()
        peer=self.servers['alice'].service.peer
        original=peer.post
        dropped=[False]
        def lose(path,payload):
            reply=original(path,payload)
            if path=='/pipeline' and payload.get('kind')=='commit' and not dropped[0]:
                dropped[0]=True
                raise ConnectionError('synthetic lost acknowledgment')
            return reply
        with patch.object(peer,'post',side_effect=lose):
            with self.assertRaises(ValueError):self.call('alice','aopp')
        self.assertTrue(self.call('alice','status')['pending_commit'])
        self.assertEqual(self.call('bob','status')['phase'],'AOPP')
        with patch('pipeline_service.pairs_for',side_effect=AssertionError('must not rerun')):
            self.assertEqual(self.call('alice','aopp')['phase'],'AOPP')

    def test_manual_ad_agreement_legacy_pp_and_peer_authorization(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError,'Only the operator'):
            self.clients['bob'].post('/pipeline',dict(action='exchange',run_id='case',kind='proposal'))
        alice=self.servers['alice'].service
        with self.assertRaisesRegex(ValueError,'selectable PP'):
            alice.peer.post('/pipeline',dict(action='exchange',run_id='case',kind='proposal',proposal=dict(
                phase='AOPP',version=0,length=258,options={'n':96,'q_est':.02,'alpha':1.0},body={'PP':2,'RKBP':[[0,1]]})))
        self.call('alice','aopp')
        self.call('alice','select_ad',b=3)
        with self.assertRaisesRegex(ValueError,'same AD size'):
            self.call('alice','ad')
        self.assertEqual(self.call('alice','status')['phase'],'AOPP')
        self.call('bob','select_ad',b=3)
        self.assertEqual(self.call('alice','ad')['phase'],'AD')

    def test_real_acceptance_gate_and_retired_run_ids(self):
        with self.assertRaisesRegex(ValueError,'Real SNS-QKD acceptance is gated'):
            self.call('alice','prepare',input_kind='real')
        self.prepare()
        self.assertTrue(self.call('alice','erase')['erased'])
        with self.assertRaisesRegex(ValueError,'already exists'):
            self.call('alice','prepare',input_kind='synthetic')


class SeparateProcessTests(unittest.TestCase):
    def test_pipeline_in_two_processes_with_bob_restart(self):
        source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'private'
            create(root,alice_port=free_port(),bob_port=free_port())
            c=load_config(root/'operator/config.json')
            clients={r:Client(c[r+'_url'],c['ca'],c['cert'],c['key'],c[r+'_cert'],timeout=180) for r in ('alice','bob')}
            a='01'*516
            b='10'+a[2:]
            for role,bits in (('alice',a),('bob',b)):
                (root/role/'DATA.txt').write_text(bits)
            processes={}
            def start(role):
                kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
                process=subprocess.Popen([sys.executable,str(source/(role+'.py')),'--config',str(root/role/'config.json')],cwd=source,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs)
                processes[role]=process
                deadline=time.monotonic()+20
                while True:
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode())
                    try:
                        clients[role].post('/health',{})
                        return
                    except OSError:
                        if time.monotonic()>deadline:raise TimeoutError('Process readiness failed')
                        time.sleep(.05)
            def stop(role):
                process=processes.pop(role)
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close();process.stderr.close()
            def call(role,action,**kw):
                return clients[role].post('/pipeline',dict(action=action,run_id='process-case',**kw))
            try:
                start('bob');start('alice')
                self.assertNotEqual(processes['alice'].pid,processes['bob'].pid)
                for role in ('alice','bob'):call(role,'prepare',input_kind='synthetic')
                first=call('alice','aopp')
                stop('bob');start('bob')
                self.assertEqual(call('bob','status')['phase'],'AOPP')
                for role in ('alice','bob'):call(role,'select_ad',b=2)
                second=call('alice','ad')
                final=call('alice','sbi')
                peer=call('bob','status')
                self.assertEqual(final['phase'],'VERIFIED')
                self.assertEqual(final['summaries'],peer['summaries'])
                output=[(root/role/'pipeline-state/process-case.bits').read_text() for role in ('alice','bob')]
                self.assertEqual(output[0],output[1])
                self.assertEqual(len(output[0]),second['current_length'])
                self.assertLess(second['current_length'],first['current_length'])
            finally:
                for role in list(processes):stop(role)
