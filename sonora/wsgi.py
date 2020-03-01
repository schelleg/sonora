import base64
from collections import namedtuple
import time
from urllib.parse import quote

import grpc

from sonora import protocol


_HandlerCallDetails = namedtuple(
    "_HandlerCallDetails", ("method", "invocation_metadata")
)


class grpcWSGI(grpc.Server):
    """
    WSGI Application Object that understands gRPC-Web.

    This is called by the WSGI server that's handling our actual HTTP
    connections. That means we can't use the normal gRPC I/O loop etc.
    """

    def __init__(self, application=None):
        self._application = application
        self._handlers = []

    def add_generic_rpc_handlers(self, handlers):
        self._handlers.extend(handlers)

    def add_insecure_port(self, port):
        raise NotImplementedError()

    def add_secure_port(self, port):
        raise NotImplementedError()

    def start(self):
        raise NotImplementedError()

    def stop(self):
        raise NotImplementedError()

    def _get_rpc_handler(self, environ):
        path = environ["PATH_INFO"]

        handler_call_details = _HandlerCallDetails(path, None)

        rpc_handler = None
        for handler in self._handlers:
            rpc_handler = handler.service(handler_call_details)
            if rpc_handler:
                return rpc_handler

        return None

    def _create_context(self, environ):
        try:
            timeout = protocol.parse_timeout(environ["HTTP_GRPC_TIMEOUT"])
        except KeyError:
            timeout = None

        metadata = []
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                header = key[5:].lower().replace("_", "-")

                if header.endswith("-bin"):
                    value = base64.b64decode(value)

                metadata.append((header, value))

        return ServicerContext(timeout, metadata)

    def _do_grpc_request(self, rpc_method, environ, start_response):
        request_data = self._read_request(environ)

        context = self._create_context(environ)

        _, _, message = protocol.unwrap_message(request_data)
        request_proto = rpc_method.request_deserializer(message)

        resp = None

        try:
            if not rpc_method.request_streaming and not rpc_method.response_streaming:
                resp = rpc_method.unary_unary(request_proto, context)
            elif not rpc_method.request_streaming and rpc_method.response_streaming:
                resp = rpc_method.unary_stream(request_proto, context)
                if context.time_remaining() is not None:
                    resp = _timeout_generator(context, resp)
            else:
                raise NotImplementedError()
        except grpc.RpcError:
            pass

        headers = [
            ("Content-Type", "application/grpc-web+proto"),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Expose-Headers", "*"),
        ]

        if rpc_method.response_streaming:
            yield from self._do_streaming_response(
                rpc_method, start_response, context, headers, resp
            )

        else:
            yield from self._do_unary_response(
                rpc_method, start_response, context, headers, resp
            )

    def _do_streaming_response(
        self, rpc_method, start_response, context, headers, resp
    ):

        try:
            first_message = next(resp)
        except grpc.RpcError:
            pass

        if context._initial_metadata:
            headers.extend(protocol.encode_headers(context._initial_metadata))

        start_response(_grpc_status_to_wsgi_status(context.code), headers)

        yield protocol.wrap_message(
            False, False, rpc_method.response_serializer(first_message)
        )

        try:
            for message in resp:
                yield protocol.wrap_message(
                    False, False, rpc_method.response_serializer(message)
                )
        except grpc.RpcError:
            pass

        trailers = [("grpc-status", str(context.code.value[0]))]

        if context.details:
            trailers.append(("grpc-message", quote(context.details)))

        if context._trailing_metadata:
            trailers.extend(protocol.encode_headers(context._trailing_metadata))

        trailer_message = protocol.pack_trailers(trailers)

        yield protocol.wrap_message(True, False, trailer_message)

    def _do_unary_response(self, rpc_method, start_response, context, headers, resp):
        if resp:
            message_data = protocol.wrap_message(
                False, False, rpc_method.response_serializer(resp)
            )
        else:
            message_data = b""

        if context._trailing_metadata:
            trailers = protocol.encode_headers(context._trailing_metadata)
            trailer_message = protocol.pack_trailers(trailers)
            trailer_data = protocol.wrap_message(True, False, trailer_message)
        else:
            trailer_data = b""
            
        content_length = len(message_data) + len(trailer_data)

        headers.append(("content-length", str(content_length)))

        headers.append(("grpc-status", str(context.code.value[0])))

        if context.details:
            headers.append(("grpc-message", quote(context.details)))

        if context._initial_metadata:
            headers.extend(protocol.encode_headers(context._initial_metadata))

        start_response(_grpc_status_to_wsgi_status(context.code), headers)
        yield message_data
        yield trailer_data

    def _do_cors_preflight(self, environ, start_response):
        start_response(
            "204 No Content",
            [
                ("Content-Type", "text/plain"),
                ("Content-Length", "0"),
                ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                ("Access-Control-Allow-Headers", "*"),
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Credentials", "true"),
                ("Access-Control-Expose-Headers", "*"),
            ],
        )
        return []

    def __call__(self, environ, start_response):
        """
        Our actual WSGI request handler. Will execute the request
        if it matches a configured gRPC service path or fall through
        to the next application.
        """

        rpc_method = self._get_rpc_handler(environ)
        request_method = environ["REQUEST_METHOD"]

        if rpc_method:
            if request_method == "POST":
                return self._do_grpc_request(rpc_method, environ, start_response)
            elif request_method == "OPTIONS":
                return self._do_cors_preflight(environ, start_response)
            else:
                start_response("400 Bad Request", [])
                return []

        if self._application:
            return self._application(environ, start_response)
        else:
            start_response("404 Not Found", [])
            return []

    def _read_request(self, environ):
        try:
            content_length = environ.get("CONTENT_LENGTH")
            if content_length:
                content_length = int(content_length)
            else:
                content_length = None
        except ValueError:
            content_length = None

        stream = environ["wsgi.input"]

        transfer_encoding = environ.get("HTTP_TRANSFER_ENCODING")

        if transfer_encoding == "chunked":
            buffer = []
            line = stream.readline()

            while line:
                if not line:
                    break

                size = line.split(b";", 1)[0]

                if size == "\r\n":
                    break

                chunk_size = int(size, 16)

                if chunk_size == 0:
                    break

                buffer.append(stream.read(chunk_size + 2)[:-2])
                line = stream.readline()
            return b"".join(buffer)
        else:
            return stream.read(content_length or 5)


class ServicerContext(grpc.ServicerContext):
    def __init__(self, timeout=None, metadata=None):
        self.code = grpc.StatusCode.OK
        self.details = None

        self._timeout = timeout

        if timeout is not None:
            self._deadline = time.monotonic() + timeout
        else:
            self._deadline = None

        self._invocation_metadata = metadata or tuple()
        self._initial_metadata = None
        self._trailing_metadata = None

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details

    def abort(self, code, details):
        if code == grpc.StatusCode.OK:
            raise ValueError()

        self.set_code(code)
        self.set_details(details)

        raise grpc.RpcError()

    def abort_with_status(self, status):
        if status == grpc.StatusCode.OK:
            raise ValueError()

        self.set_code(status)

        raise grpc.RpcError()

    def time_remaining(self):
        if self._deadline is not None:
            return max(self._deadline - time.monotonic(), 0)
        else:
            return None

    def invocation_metadata(self):
        return self._invocation_metadata

    def send_initial_metadata(self, initial_metadata):
        self._initial_metadata = initial_metadata

    def set_trailing_metadata(self, trailing_metadata):
        self._trailing_metadata = trailing_metadata

    def peer(self):
        raise NotImplementedError()

    def peer_identities(self):
        raise NotImplementedError()

    def peer_identity_key(self):
        raise NotImplementedError()

    def auth_context(self):
        raise NotImplementedError()

    def add_callback(self):
        raise NotImplementedError()

    def cancel(self):
        raise NotImplementedError()

    def is_active(self):
        raise NotImplementedError()


def _grpc_status_to_wsgi_status(code):
    if code == grpc.StatusCode.OK:
        return "200 OK"
    elif code is None:
        return "200 OK"
    elif code == grpc.StatusCode.UNKNOWN:
        return "500 Internal Server Error"
    elif code == grpc.StatusCode.INTERNAL:
        return "500 Internal Server Error"
    elif code == grpc.StatusCode.UNAVAILABLE:
        return "503 Service Unavailable"
    elif code == grpc.StatusCode.INVALID_ARGUMENT:
        return "400 Bad Request"
    elif code == grpc.StatusCode.UNIMPLEMENTED:
        return "404 Not Found"
    elif code == grpc.StatusCode.PERMISSION_DENIED:
        return "403 Forbidden"
    else:
        return "500 Internal Server Error"


def _timeout_generator(context, gen):
    while 1:
        if context.time_remaining() > 0:
            yield next(gen)
        else:
            context.code = grpc.StatusCode.DEADLINE_EXCEEDED
            context.details = "request timed out at the server"
            raise grpc.RpcError()
