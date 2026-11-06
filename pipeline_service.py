"""Endpoint-owned, durable layer checkpoints around the existing SBI engine.

Interrupted SBI attempts restart from the AD checkpoint, not a partially decoded
candidate. Layer commit retries are idempotent; simultaneous commits are not claimed.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from protocol import choose_spec, make_pool, digest
from stages import parse_key, pairs_for, check_pairs, retain_pairs, validate_ad, mask_blocks, check_blocks, retain_blocks


class Pipeline:
    def __init__(self, service):
        self.service=service
        self.config=service.config
        self.role=service.role
        self.lock=threading.RLock()
        self.root=Path(self.config.get('state_dir',str(Path(self.config.get('output_dir','output')).parent/'pipeline-state')))
        self.root.mkdir(parents=True,exist_ok=True)
        self.db=self.root/'checkpoints.sqlite'
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, state TEXT NOT NULL)')
        self.db.chmod(0o600)

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.db,timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self, run):
        with self.connection() as db:
            row=db.execute('SELECT state FROM runs WHERE id=?',(run,)).fetchone()
        if not row:
            raise ValueError('Unknown run; load this endpoint input first')
        return json.loads(row[0])

    def save(self, state):
        state['updated']=time.time()
        with self.connection() as db:
            db.execute('INSERT INTO runs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state',
                       (state['run_id'],json.dumps(state,separators=(',',':'),allow_nan=False)))

    def event(self,state,stage,message,**details):
        state['events'].append(dict(stage=stage,message=message,**details))
        state['events']=state['events'][-150:]

    def public(self,state):
        return {k:state.get(k) for k in ('run_id','phase','version','input_length','summaries','events','selected_b','options','updated')} | {
            'role':self.role,'current_length':len(state.get('bits','')) if state['phase']!='VERIFIED' else state['summaries'][-1]['output_bits'],
            'pending_commit':bool(state.get('pending')),'cleanup':'Intermediate phase payloads removed after acknowledged commit; checkpoint retained until explicit erase',
            'purpose':'synthetic-validation','real_key_acceptance':False}

    def dispatch(self,payload,identity):
        if not isinstance(payload,dict):
            raise ValueError('Expected JSON object')
        action=payload.get('action')
        if ((action=='exchange' and not (identity=='peer' and self.role=='bob'))
                or (action!='exchange' and identity!='operator')):
            raise PermissionError('Only the operator controls runs; only Alice may send pipeline exchanges to Bob')
        run=payload.get('run_id')
        if not isinstance(run,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,48}',run):
            raise ValueError('Run ID must contain 1..48 letters, digits, underscores or hyphens')
        if action=='upload' and identity=='operator':
            if payload.get('input_kind')!='synthetic':
                raise ValueError('Real SNS-QKD acceptance is gated; use synthetic fixtures only')
            bits=parse_key(payload.get('text'))
            path=Path(self.config.get('input_file',str(Path(self.config.get('output_dir','output')).parent/'DATA.txt')))
            path.parent.mkdir(parents=True,exist_ok=True)
            import tempfile
            temporary=None
            try:
                with tempfile.NamedTemporaryFile(mode='w',encoding='ascii',dir=path.parent,prefix='.input-',suffix='.tmp',delete=False) as stream:
                    temporary=Path(stream.name)
                    stream.write(bits+'\n')
                os.replace(temporary,path)
            finally:
                if temporary is not None:temporary.unlink(missing_ok=True)
            return {'role':self.role,'uploaded_bits':len(bits),'filename':'DATA.txt','purpose':'synthetic-validation'}
        if action=='status' and identity=='operator':
            try:
                state=self.load(run)
            except ValueError:
                return dict(run_id=run,role=self.role,phase='NOT_LOADED',input_length=0,current_length=0,
                            summaries=[],events=[],pending_commit=False,real_key_acceptance=False)
            return self.public(state)
        with self.lock:
            if action=='prepare':
                if payload.get('input_kind')!='synthetic':
                    raise ValueError('Real SNS-QKD acceptance is gated pending input-stage metadata, operating limits, estimator and failure budget')
                options=payload.get('options',{'n':96,'q_est':.02,'alpha':1.0})
                if not isinstance(options,dict) or set(options)!={'n','q_est','alpha'}:
                    raise ValueError('Specify n, q_est and alpha')
                choose_spec(make_pool(options['n']),options['q_est'],options['alpha'])
                path=Path(self.config.get('input_file',str(Path(self.config.get('output_dir','output')).parent/'DATA.txt')))
                if path.stat().st_size>1000000:
                    raise ValueError('Input file exceeds 1 MB')
                bits=parse_key(path.read_text(encoding='utf-8-sig'))
                with self.connection() as db:
                    if db.execute('SELECT 1 FROM runs WHERE id=?',(run,)).fetchone():
                        raise ValueError('Run ID already exists; use status or a new ID')
                state=dict(run_id=run,phase='RAW',version=0,input_length=len(bits),bits=bits,
                           positions=list(range(len(bits))),summaries=[],events=[],options=options,selected_b=None)
                self.event(state,'Input','Loaded local synthetic DATA.txt',bits=len(bits))
                self.save(state)
                return self.public(state)
            state=self.load(run)
            if state['phase']=='ERASED':
                raise ValueError('Run was erased; ID is permanently retired')
            if action=='status':
                return self.public(state)
            if action=='erase':
                if state.get('pending'):
                    raise ValueError('Resolve the pending peer commit before erasing this run')
                self.clear_attempt(state)
                state={k:state[k] for k in ('run_id','version','input_length','summaries','options','events')}
                state.update(phase='ERASED',bits='',positions=[],selected_b=None)
                self.event(state,'Cleanup','Private checkpoint references removed; original DATA.txt and accepted outputs retained')
                self.save(state)
                return {'erased':True,'secure_erasure':False}
            if action=='select_ad':
                if state['phase']!='AOPP' or state.get('pending'):
                    raise ValueError('AD selection requires an acknowledged AOPP checkpoint')
                if state.get('draft') and payload.get('b')!=state['draft']['body'].get('b'):
                    raise ValueError('An earlier AD proposal may be pending at the peer; resolve it before changing b')
                validate_ad(len(state['bits']),payload.get('b'))
                state['selected_b']=payload['b']
                self.save(state)
                return self.public(state)
            if action=='exchange':
                return self.receive(state,payload)
            if self.role!='alice':
                raise ValueError('Start layer execution on Alice; Bob responds over mTLS')
            if action in ('aopp','ad'):
                return self.layer(state,action.upper())
            if action=='sbi':
                return self.sbi(state)
            raise ValueError('Unknown pipeline action')

    def send(self,state,kind,**fields):
        return self.service.peer.post('/pipeline',dict(action='exchange',run_id=state['run_id'],kind=kind,**fields))

    def summary(self,state,phase,bits,positions,**fields):
        return dict(stage=phase,input_bits=len(state['bits']),output_bits=len(bits),
                    retained_positions_digest=digest(positions),**fields)

    def stage(self,state,phase,bits,positions,summary):
        state['pending']=dict(phase=phase,bits=bits,positions=positions,summary=summary)
        self.save(state)

    def apply(self,state):
        original_state=state
        state=json.loads(json.dumps(state))
        pending=state.pop('pending')
        state.update(phase=pending['phase'],bits=pending['bits'],positions=pending['positions'],version=state['version']+1)
        state['summaries'].append(pending['summary'])
        state['last_commit']=dict(phase=state['phase'],summary=pending['summary'])
        for key in ('draft','proposal','proposal_response','sbi_attempt'):
            state.pop(key,None)
        self.event(state,state['phase'],'Peer length and ordered-position mapping acknowledged',input_bits=pending['summary']['input_bits'],output_bits=len(state['bits']))
        if state['phase']=='VERIFIED':
            target=self.root/(state['run_id']+'.bits')
            if target.exists():
                if target.read_text()!=state['bits']:
                    raise ValueError('Existing verified output differs; abort without overwrite')
            else:
                import tempfile
                temporary=None
                try:
                    with tempfile.NamedTemporaryFile(mode='w',encoding='ascii',dir=self.root,prefix='.verified-',suffix='.tmp',delete=False) as stream:
                        temporary=Path(stream.name)
                        stream.write(state['bits'])
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Atomic, no-overwrite publication on the required local filesystem.
                    if os.name=='nt':
                        os.rename(temporary,target)  # Windows rename refuses existing targets.
                    else:
                        os.link(temporary,target)
                finally:
                    if temporary is not None:temporary.unlink(missing_ok=True)
            state['bits']=''
            state['positions']=[]
        self.save(state)
        original_state.clear()
        original_state.update(state)

    def commit(self,state):
        pending=state['pending']
        reply=self.send(state,'commit',phase=pending['phase'],summary=pending['summary'])
        if reply!={'committed':pending['phase'],'summary':pending['summary']}:
            raise ValueError('Peer commit acknowledgment mismatch; local pending checkpoint retained')
        self.apply(state)
        return self.public(state)

    def layer(self,state,phase):
        expected='RAW' if phase=='AOPP' else 'AOPP'
        if state.get('pending'):
            if state['pending']['phase']!=phase:
                raise ValueError('A different layer is awaiting commit')
            return self.commit(state)
        if state['phase']==phase:
            return self.public(state)
        if state['phase']!=expected:
            raise ValueError('Layer is out of order')
        if phase=='AD':
            validate_ad(len(state['bits']),state['selected_b'])
        if 'draft' not in state:
            body={'RKBP':pairs_for(state['bits'])} if phase=='AOPP' else {'b':state['selected_b'],'masked':mask_blocks(state['bits'],state['selected_b'])}
            state['draft']=dict(phase=phase,version=state['version'],length=len(state['bits']),options=state['options'],body=body)
            self.save(state)
        draft=state['draft']
        response=self.send(state,'proposal',proposal=draft)
        body=draft['body']
        if phase=='AOPP':
            bits,positions=retain_pairs(state['bits'],state['positions'],body['RKBP'],response['flags'])
            summary=self.summary(state,phase,bits,positions,groups=len(body['RKBP']),accepted=len(bits),unmatched=len(state['bits'])-2*len(body['RKBP']))
        else:
            bits,positions=retain_blocks(state['bits'],state['positions'],body['b'],response['flags'])
            summary=self.summary(state,phase,bits,positions,b=body['b'],groups=len(response['flags']),accepted=len(bits))
        if response['summary']!=summary:
            raise ValueError('Peer retained positions or counts differ')
        self.stage(state,phase,bits,positions,summary)
        return self.commit(state)

    def receive(self,state,payload):
        kind=payload.get('kind')
        if kind=='commit':
            expected=dict(phase=payload.get('phase'),summary=payload.get('summary'))
            if state.get('last_commit')==expected:
                return dict(committed=expected['phase'],summary=expected['summary'])
            if not state.get('pending') or any(state['pending'][k]!=expected[k] for k in expected):
                raise ValueError('No matching pending phase to commit')
            self.apply(state)
            return dict(committed=expected['phase'],summary=expected['summary'])
        if kind=='proposal':
            proposal=payload.get('proposal')
            if 'proposal' in state and state['proposal']==proposal:
                return state['proposal_response']
            if not isinstance(proposal,dict) or set(proposal)!={'phase','version','length','options','body'}:
                raise ValueError('Invalid phase proposal')
            phase=proposal['phase']
            if state.get('pending') or state['phase']!=('RAW' if phase=='AOPP' else 'AOPP') or phase not in ('AOPP','AD'):
                raise ValueError('Proposal is stale or out of order')
            if (proposal['version'],proposal['length'],proposal['options'])!=(state['version'],len(state['bits']),state['options']):
                raise ValueError('Input lengths, versions or SBI settings differ')
            body=proposal['body']
            if phase=='AOPP':
                if not isinstance(body,dict) or set(body)!={'RKBP'}:
                    raise ValueError('AOPP accepts RKBP only; selectable PP is unsupported')
                flags=check_pairs(state['bits'],body['RKBP'])
                bits,positions=retain_pairs(state['bits'],state['positions'],body['RKBP'],flags)
                summary=self.summary(state,phase,bits,positions,groups=len(flags),accepted=len(bits),unmatched=len(state['bits'])-2*len(flags))
            else:
                if not isinstance(body,dict) or set(body)!={'b','masked'} or body['b']!=state['selected_b']:
                    raise ValueError('Both parties must manually select the same AD size first')
                flags=check_blocks(state['bits'],body['b'],body['masked'])
                bits,positions=retain_blocks(state['bits'],state['positions'],body['b'],flags)
                summary=self.summary(state,phase,bits,positions,b=body['b'],groups=len(flags),accepted=len(bits))
            state['pending']=dict(phase=phase,bits=bits,positions=positions,summary=summary)
            response=dict(flags=flags,summary=summary)
            state['proposal']=proposal
            state['proposal_response']=response
            self.save(state)
            return response
        if kind=='sbi_begin':
            if state['phase']!='AD' or state.get('pending'):
                raise ValueError('SBI requires committed AD output')
            if payload.get('length')!=len(state['bits']) or payload.get('options')!=state['options']:
                raise ValueError('SBI input/settings mismatch')
            attempt_id=payload.get('attempt')
            if (not isinstance(attempt_id,str) or not re.fullmatch('[a-f0-9]{32}',attempt_id)
                    or payload.get('version')!=state['version'] or attempt_id in state.get('retired_attempts',[])):
                raise ValueError('Invalid, stale or retired SBI attempt')
            if state.get('sbi_attempt',{}).get('id')==attempt_id:
                return {'ready':True}
            self.clear_attempt(state)
            state['sbi_attempt']=dict(id=payload['attempt'],candidate='',next=0,sessions=[])
            self.save(state)
            return {'ready':True}
        if kind=='sbi_block':
            attempt=state.get('sbi_attempt')
            if not attempt or attempt['id']!=payload.get('attempt') or payload.get('block')!=attempt['next']:
                raise ValueError('Wrong SBI attempt or block order')
            index=payload['block']
            spec,raw=self.block(state,index)
            if payload.get('spec_digest')!=digest(spec):
                raise ValueError('SBI code/packing mismatch')
            sid=self.block_id(state,attempt['id'],index)
            self.service.execute('/prepare',dict(session_id=sid,spec=spec,raw_key=list(map(int,raw))),'operator')
            self.service.sessions[sid]['defer_output']=True
            attempt['sessions'].append(sid)
            self.save(state)
            return {'session_id':sid}
        if kind=='sbi_accept':
            attempt=state.get('sbi_attempt')
            if not attempt or attempt['id']!=payload.get('attempt') or payload.get('block')!=attempt['next']:
                raise ValueError('Unexpected SBI block acceptance')
            sid=self.block_id(state,attempt['id'],attempt['next'])
            party=self.service.sessions.get(sid,{}).get('party')
            if party is None or party.status!='verified':
                raise ValueError('Block has not passed authenticated verification')
            attempt['candidate']+=''.join(str(int(x)) for x in party.corrected)
            attempt['next']+=1
            self.service.sessions.pop(sid,None)
            self.save(state)
            return {'accepted':attempt['next']}
        if kind=='sbi_finish':
            attempt=state.get('sbi_attempt')
            if not attempt or attempt['id']!=payload.get('attempt') or len(attempt['candidate'])!=len(state['bits']):
                raise ValueError('SBI candidate is incomplete')
            summary=self.summary(state,'SBI',attempt['candidate'],state['positions'],blocks=attempt['next'],verification_bits=64*attempt['next'])
            self.stage(state,'VERIFIED',attempt['candidate'],state['positions'],summary)
            return {'summary':summary}
        raise ValueError('Unknown peer pipeline operation')

    def clear_attempt(self,state):
        previous=state.get('sbi_attempt',{}).get('id')
        if previous and previous not in state.setdefault('retired_attempts',[]):
            state['retired_attempts'].append(previous)
        for sid in state.get('sbi_attempt',{}).get('sessions',[]):
            self.service.sessions.pop(sid,None)
        state.pop('sbi_attempt',None)

    def block_id(self,state,attempt,index):
        return hashlib.sha256((state['run_id']+':'+attempt+':'+str(index)).encode()).hexdigest()[:32]

    def block(self,state,index):
        options=state['options']
        spec=choose_spec(make_pool(options['n']),options['q_est'],options['alpha'])
        positions=[i for i in range(options['n']) if i not in set(spec['S']+spec['P'])]
        raw=state['bits'][index*len(positions):(index+1)*len(positions)]
        if not raw:
            raise ValueError('SBI block is outside the input')
        spec['S']=sorted(spec['S']+positions[len(raw):])
        return spec,raw

    def sbi(self,state):
        if state.get('pending'):
            if state['pending']['phase']!='VERIFIED':
                raise ValueError('A different layer is pending')
            return self.commit(state)
        if state['phase']=='VERIFIED':
            return self.public(state)
        if state['phase']!='AD' or not state['bits']:
            raise ValueError('SBI requires nonempty committed AD output')
        # New random attempt IDs retire incomplete native decoder sessions. AD is
        # preserved; mid-block decoder resume is deliberately not advertised.
        self.clear_attempt(state)
        attempt=secrets.token_hex(16)
        state['sbi_attempt']=dict(id=attempt,sessions=[])
        self.save(state)
        self.send(state,'sbi_begin',attempt=attempt,length=len(state['bits']),options=state['options'],version=state['version'])
        candidate=''
        block=0
        try:
            while len(candidate)<len(state['bits']):
                spec,raw=self.block(state,block)
                sid=self.block_id(state,attempt,block)
                self.service.execute('/prepare',dict(session_id=sid,spec=spec,raw_key=list(map(int,raw))),'operator')
                state['sbi_attempt']['sessions'].append(sid)
                self.save(state)
                reply=self.send(state,'sbi_block',attempt=attempt,block=block,spec_digest=digest(spec))
                if reply!={'session_id':sid}:
                    raise ValueError('Peer block identity mismatch')
                result=self.service.execute('/run',{'session_id':sid},'operator')
                if result['summary']['status']!='verified':
                    raise ValueError('SBI verification failed; AD checkpoint retained')
                party=self.service.sessions[sid]['party']
                candidate+=''.join(str(int(x)) for x in party.corrected)
                self.send(state,'sbi_accept',attempt=attempt,block=block)
                self.service.sessions.pop(sid,None)
                block+=1
                self.event(state,'SBI','Verified block',block=block,retained_bits=len(raw),additional_rounds=result['summary']['additional_rounds'])
                self.save(state)
            summary=self.summary(state,'SBI',candidate,state['positions'],blocks=block,verification_bits=64*block)
            reply=self.send(state,'sbi_finish',attempt=attempt)
            if reply!={'summary':summary}:
                raise ValueError('SBI final length/mapping mismatch')
            self.stage(state,'VERIFIED',candidate,state['positions'],summary)
            return self.commit(state)
        except Exception:
            self.clear_attempt(state)
            self.event(state,'SBI','Attempt aborted; unverified output rejected, AD checkpoint retained')
            self.save(state)
            raise
