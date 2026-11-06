"""One-role control of the endpoint-owned pipeline. Never reads either private key."""
import argparse
import json
from transport import Client,load_config

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','status','aopp','select_ad','ad','sbi','erase'])
    p.add_argument('--config',required=True)
    p.add_argument('--party',choices=['alice','bob'],required=True)
    p.add_argument('--run',required=True)
    p.add_argument('--b',type=int)
    a=p.parse_args()
    c=load_config(a.config)
    client=Client(c[a.party+'_url'],c['ca'],c['cert'],c['key'],c[a.party+'_cert'],timeout=180)
    payload=dict(action=a.action,run_id=a.run)
    if a.action=='prepare':payload['input_kind']='synthetic'
    if a.action=='select_ad':payload['b']=a.b
    print(json.dumps(client.post('/pipeline',payload),indent=2))

if __name__=='__main__':main()
