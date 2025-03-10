import getpass
import select
import socket
from datetime import datetime

from .commands import (
    client_ident_command,
    define_word_command,
    disconnect_command,
    help_command,
    match_command,
    show_databases_command,
    show_info_command,
    show_server_command,
    show_strategies_command,
    status_command,
)
from .response import (
    DatabaseInfoResponse,
    DefineWordResponse,
    HandshakeResponse,
    MatchResponse,
    MultiLineResponse,
    PreliminaryResponse,
    ServerPropertiesResponse,
)
from .status_codes import DictStatusCode
from .word import Word

BUF_SIZE = 4096
DEFAULT_PORT = 2628


class ReadOnlyDescriptor:
    def __set_name__(self, owner, name):
        self.public_name = name
        self.private_name = f"_{name}"

    def raise_read_only(self, obj):
        raise AttributeError(
            f"{obj.__class__.__name__}.{self.public_name} is a read-only attribute"
        )

    def __set__(self, obj, value):
        self.raise_read_only(obj)

    def __delete__(self, obj):
        self.raise_read_only(obj)


class Strategies(ReadOnlyDescriptor):
    def __get__(self, obj, obj_type=None):
        if not getattr(obj, self.private_name, None):
            obj.sock.sendall(show_strategies_command())
            response = ServerPropertiesResponse(obj._recv_all())
            setattr(obj, self.private_name, response.content)
        return getattr(obj, self.private_name)


class Databases(ReadOnlyDescriptor):
    def __get__(self, obj, obj_type=None):
        if not getattr(obj, self.private_name, None):
            obj.sock.sendall(show_databases_command())
            response = ServerPropertiesResponse(obj._recv_all())
            setattr(obj, self.private_name, response.content)
        return getattr(obj, self.private_name)


class DictionaryClient:
    """Implements a client for communication with a server implementing
    the DICT Server Protocol (https://tools.ietf.org/html/rfc2229).
    """

    strategies = Strategies()
    databases = Databases()

    def __init__(self, host="localhost", port=DEFAULT_PORT, sock_class=socket.socket):
        self.client_name = f"{getpass.getuser()}@{socket.gethostname()}"
        self.client_id_info = f"{self.client_name} {datetime.now().isoformat()}"
        self.sock = sock_class(socket.AF_INET, socket.SOCK_STREAM)
        self.server_info = self._connect(host, port)

    def _recv_all(self):
        rlist, _, _ = select.select([self.sock], [], [], 5)
        if self.sock not in rlist:
            raise TimeoutError("Client timed out expecting server response.")
        bytes_received = self.sock.recv(BUF_SIZE)
        status_code = self._get_status(bytes_received)
        if DictStatusCode.response_complete(status_code):
            return bytes_received
        while not self._response_complete(bytes_received):
            rlist, _, _ = select.select([self.sock], [], [], 5)
            if self.sock not in rlist:
                raise TimeoutError(
                    "Client timed out following preliminary response with status "
                    f"{status_code}."
                )
            bytes_received += self.sock.recv(BUF_SIZE)
        return bytes_received

    def _connect(self, host, port):
        self.sock.connect((host, port))
        response = HandshakeResponse(self._recv_all())
        if response.status_code != DictStatusCode.CONNECTION_ACCEPTED:
            raise Exception(response.status_code)
        self._send_client_ident()
        return response.content

    def _send_client_ident(self):
        self.sock.sendall(client_ident_command(self.client_id_info))
        response = PreliminaryResponse(self._recv_all())
        if response.status_code != DictStatusCode.OK:
            raise Exception(response.status_code)

    def _get_status(self, response_bytes):
        return int(response_bytes[:3])

    def _response_complete(self, response_bytes):
        return (response_bytes.startswith(b"250") or b"\r\n250" in response_bytes) and response_bytes[-2:] == b"\r\n"

    def _get_response(self, command, response_class):
        self.sock.sendall(command)
        return response_class(self._recv_all())

    def get_server_status(self):
        return self._get_response(status_command(), PreliminaryResponse)

    def get_server_information(self):
        return self._get_response(show_server_command(), MultiLineResponse)

    def get_db_info(self, db):
        if db not in self.databases:
            raise ValueError(f'Invalid database name: "{db}" not present.')
        return self._get_response(show_info_command(db), DatabaseInfoResponse)

    def get_help_text(self):
        return self._get_response(help_command(), MultiLineResponse)

    def define(self, word_raw, db="*"):
        if db != "*" and db not in self.databases:
            raise ValueError(f'Invalid database name: "{db}" not present.')
        word = Word(word_raw)
        return self._get_response(define_word_command(word, db), DefineWordResponse)

    def match(self, word_raw, db="*", strategy="."):
        if db != "*" and db not in self.databases:
            raise ValueError(f'Invalid database name: "{db}" not present.')
        if strategy != "." and strategy not in self.strategies:
            raise ValueError(f'Unknown strategy: "{strategy}".')
        word = Word(word_raw)
        return self._get_response(
            match_command(word, db=db, strategy=strategy), MatchResponse
        )

    def disconnect(self):
        self.sock.sendall(disconnect_command())
        bytes_recieved = self._recv_all()
        if self._get_status(bytes_recieved) != DictStatusCode.CLOSING_CONNECTION:
            raise ConnectionError(
                "Client got unexpected response to QUIT command: "
                f'"{bytes_recieved.decode()}"'
            )
        self.sock.close()
