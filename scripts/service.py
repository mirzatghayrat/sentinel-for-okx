"""User-operated macOS LaunchAgent installer. Never run by the development agent."""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
LABEL='local.okx-desk'
PLIST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'


def main():
    parser=argparse.ArgumentParser(description='Install/remove the Sentinel for OKX login service on your own Mac')
    parser.add_argument('action',choices=['install','uninstall'])
    parser.add_argument('--allow-live',action='store_true')
    args=parser.parse_args()
    if sys.platform!='darwin': raise SystemExit('仅适用于 macOS')
    target=f'gui/{os.getuid()}'
    if args.action=='uninstall':
        subprocess.run(['launchctl','bootout',f'{target}/{LABEL}'],check=False)
        if PLIST.exists(): PLIST.unlink()
        print('登录服务已移除；账户数据和密钥仍保留在 data/。')
        return
    python=ROOT/'.venv/bin/python'
    if not python.exists(): raise SystemExit('请先在项目目录运行 uv sync --frozen')
    (ROOT/'data').mkdir(mode=0o700,exist_ok=True)
    arguments=['/usr/bin/caffeinate','-i',str(python),str(ROOT/'run.py')]
    if args.allow_live: arguments.append('--allow-live')
    config={'Label':LABEL,'ProgramArguments':arguments,'WorkingDirectory':str(ROOT),
            'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},'ThrottleInterval':30,
            'StandardOutPath':str(ROOT/'data/service.log'),'StandardErrorPath':str(ROOT/'data/service-error.log')}
    PLIST.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['launchctl','bootout',f'{target}/{LABEL}'],capture_output=True)
    PLIST.write_bytes(plistlib.dumps(config));PLIST.chmod(0o600)
    subprocess.run(['launchctl','bootstrap',target,str(PLIST)],check=True)
    print('已安装登录服务。程序重启后策略始终暂停，需要你在网页重新启动。')
    print('这是登录后的后台服务；完全退出登录或 FileVault 等待解锁时不能保证运行。')

if __name__=='__main__':main()
