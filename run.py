"""Run the browser application locally or as one server process."""
import argparse
import os
from pathlib import Path
import sys
import threading
import webbrowser

import uvicorn
from backend.app import create_app


def main():
    parser = argparse.ArgumentParser(description='FinGuard Web')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if args.host not in ('127.0.0.1', 'localhost', '::1') and len(os.environ.get('FINGUARD_PASSWORD', '')) < 12:
        parser.error('Для сетевого запуска задайте FINGUARD_PASSWORD длиной от 12 символов.')
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not os.environ.get('FINGUARD_ALLOWED_HOSTS'):
        parser.error('Для сетевого запуска задайте FINGUARD_ALLOWED_HOSTS: имена хостов через запятую.')
    app = create_app(args.data_dir)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level='info', workers=1, proxy_headers=False)


if __name__ == '__main__':
    main()
