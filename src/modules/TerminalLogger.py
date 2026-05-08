from datetime import datetime
import logging


class TerminalLogger(object):
    """Stream-like que envia prints para arquivo + logging unificado.

    Cada linha (terminada em \n) gera tambem um log INFO no logging padrao,
    correlacionando prints e logs no mesmo trading_bot.log.
    """
    encoding = "utf-8"

    def __init__(self, filename="src/logs/terminal.log", mirror_to_logging: bool = True):
        self.log = open(filename, "a", encoding="utf-8", buffering=1)
        self.mirror_to_logging = mirror_to_logging
        self._buf = ""

    def write(self, message):
        try:
            self.log.write(message)
            self.log.flush()
            if self.mirror_to_logging and isinstance(message, str):
                self._buf += message
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    line = line.rstrip()
                    if line:
                        try:
                            logging.getLogger("terminal").info(line)
                        except Exception:
                            pass
            return len(message) if isinstance(message, str) else 0
        except Exception:
            return 0

    def flush(self):
        try:
            self.log.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("TerminalLogger has no fileno")
