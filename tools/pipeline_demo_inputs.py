"""Create correlated synthetic endpoint files for an existing certificate setup."""
import argparse
import random
from pathlib import Path

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--private',required=True)
    a=p.parse_args()
    root=Path(a.private)
    values=list('0'*516+'1'*516)
    random.Random(161203673).shuffle(values)
    other=values.copy()
    for index in (values.index('0'),values.index('1')):
        other[index]='1' if values[index]=='0' else '0'
    for role,bits in (('alice',values),('bob',other)):
        folder=root/role
        folder.mkdir(parents=True,exist_ok=True)
        target=folder/'DATA.txt'
        with target.open('x',encoding='ascii') as f:
            f.write(''.join(bits)+'\n')
    print('Created two synthetic DATA.txt files (1032 bits each, two controlled differences). Never use these as cryptographic keys.')
