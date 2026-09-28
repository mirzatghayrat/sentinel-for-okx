"""Bind only to loopback: the desk is opened from the browser on this Mac."""
import argparse
import os
import uvicorn

if __name__ == '__main__':
    p=argparse.ArgumentParser(description='Sentinel for OKX — standalone OKX spot control desk')
    p.add_argument('--allow-live',action='store_true',help='Allow user-initiated live trading in the UI; does not start a strategy')
    args=p.parse_args()
    os.environ['OKX_DESK_ALLOW_LIVE']='1' if args.allow_live else '0'
    uvicorn.run('lab.app:app',host='127.0.0.1',port=8765,workers=1,access_log=False,proxy_headers=False)
