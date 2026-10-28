"""Local development factory; ports are bound to host loopback by Compose."""
from .app import create_app

def create_dev_app():
    return create_app(password='', allowed_hosts=['localhost', '127.0.0.1', 'backend'])
