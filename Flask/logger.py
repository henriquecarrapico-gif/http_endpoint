import os
import logging
from datetime import datetime


class GatewayLogger:
    """Structured logger for the Gateway HTTP endpoint.
    Mirrors the OutputLogger pattern from tower_mic."""

    def __init__(self, log_dir='logs', log_file_name='gateway.log', level='DEBUG', console=True):
        self.log_dir = log_dir
        self.log_file_name = log_file_name
        self.log_file = os.path.join(self.log_dir, self.log_file_name)
        self.console = console
        self.log_level = level

        # Ensure log directory exists
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)

        # Build handlers
        handlers = [
            logging.FileHandler(self.log_file)
        ]
        if self.console:
            handlers.append(logging.StreamHandler())

        # Create logger (avoid root logger collision with Flask)
        self.logger = logging.getLogger('Gateway')
        self.logger.setLevel(getattr(logging, self.log_level, logging.DEBUG))
        self.logger.handlers.clear()

        formatter = logging.Formatter(
            fmt='%(asctime)s | [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        for h in handlers:
            h.setFormatter(formatter)
            self.logger.addHandler(h)

        # Prevent propagation to Flask's default logger
        self.logger.propagate = False

    def debug(self, msg):
        self.logger.debug(msg)

    def info(self, msg):
        self.logger.info(msg)

    def warning(self, msg):
        self.logger.warning(msg)

    def error(self, msg):
        self.logger.error(msg)

    def critical(self, msg):
        self.logger.critical(msg)
