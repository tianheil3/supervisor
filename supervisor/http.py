import os
import stat
import time
import sys
import socket
import errno
import weakref
import traceback
import supervisor.options

try:
    import pwd
except ImportError:  # Windows
    import getpass as pwd

from supervisor.compat import urllib
from supervisor.compat import sha1
from supervisor.compat import as_bytes
from supervisor.compat import as_string
from supervisor.medusa import asyncore_25 as asyncore
from supervisor.medusa import http_date
from supervisor.medusa import http_server
from supervisor.medusa import producers
from supervisor.medusa import filesys
from supervisor.medusa import default_handler

from supervisor.medusa.auth_handler import auth_handler

class NOT_DONE_YET:
    pass

class deferring_chunked_producer:
    """A producer that implements the 'chunked' transfer coding for HTTP/1.1.
    Here is a sample usage:
            request['Transfer-Encoding'] = 'chunked'
            request.push (
                    producers.chunked_producer (your_producer)
                    )
            request.done()
    """

    def __init__ (self, producer, footers=None):
        self.producer = producer
        self.footers = footers
        self.delay = 0.1

    def more (self):
        if self.producer:
            data = self.producer.more()
            if data is NOT_DONE_YET:
                return NOT_DONE_YET
            elif data:
                s = '%x' % len(data)
                return as_bytes(s) + b'\r\n' + data + b'\r\n'
            else:
                self.producer = None
                if self.footers:
                    return b'\r\n'.join([b'0'] + self.footers) + b'\r\n\r\n'
                else:
                    return b'0\r\n\r\n'
        else:
            return b''

class deferring_composite_producer:
    """combine a fifo of producers into one"""
    def __init__ (self, producers):
        self.producers = producers
        self.delay = 0.1

    def more (self):
        while len(self.producers):
            p = self.producers[0]
            d = p.more()
            if d is NOT_DONE_YET:
                return NOT_DONE_YET
            if d:
                return d
            else:
                self.producers.pop(0)
        else:
            return b''


class deferring_globbing_producer:
    """
    'glob' the output from a producer into a particular buffer size.
    helps reduce the number of calls to send().  [this appears to
    gain about 30% performance on requests to a single channel]
    """

    def __init__ (self, producer, buffer_size=1<<16):
        self.producer = producer
        self.buffer = b''
        self.buffer_size = buffer_size
        self.delay = 0.1

    def more (self):
        while len(self.buffer) < self.buffer_size:
            data = self.producer.more()
            if data is NOT_DONE_YET:
                return NOT_DONE_YET
            if data:
                try:
                    self.buffer = self.buffer + data
                except TypeError:
                    self.buffer = as_bytes(self.buffer) + as_bytes(data)
            else:
                break
        r = self.buffer
        self.buffer = b''
        return r


class deferring_hooked_producer:
    """
    A producer that will call <function> when it empties,.
    with an argument of the number of bytes produced.  Useful
    for logging/instrumentation purposes.
    """

    def __init__ (self, producer, function):
        self.producer = producer
        self.function = function
        self.bytes = 0
        self.delay = 0.1

    def more (self):
        if self.producer:
            result = self.producer.more()
            if result is NOT_DONE_YET:
                return NOT_DONE_YET
            if not result:
                self.producer = None
                self.function (self.bytes)
            else:
                self.bytes += len(result)
            return result
        else:
            return b''


class deferring_http_request(http_server.http_request):
    """ The medusa http_request class uses the default set of producers in
    medusa.producers.  We can't use these because they don't know anything
    about deferred responses, so we override various methods here.  This was
    added to support tail -f like behavior on the logtail handler """

    def done(self, *arg, **kw):

        """ I didn't want to override this, but there's no way around
        it in order to support deferreds - CM

        finalize this transaction - send output to the http channel"""

        # ----------------------------------------
        # persistent connection management
        # ----------------------------------------

        #  --- BUCKLE UP! ----

        connection = http_server.get_header(http_server.CONNECTION,self.header)
        connection = connection.lower()

        close_it = 0
        wrap_in_chunking = 0
        globbing = 1

        if self.version == '1.0':
            if connection == 'keep-alive':
                if not 'Content-Length' in self:
                    close_it = 1
                else:
                    self['Connection'] = 'Keep-Alive'
            else:
                close_it = 1
        elif self.version == '1.1':
            if connection == 'close':
                close_it = 1
            elif not 'Content-Length' in self:
                if 'Transfer-Encoding' in self:
                    if not self['Transfer-Encoding'] == 'chunked':
                        close_it = 1
                elif self.use_chunked:
                    self['Transfer-Encoding'] = 'chunked'
                    wrap_in_chunking = 1
                    # globbing slows down tail -f output, so only use it if
                    # we're not in chunked mode
                    globbing = 0
                else:
                    close_it = 1
        elif self.version is None:
            # Although we don't *really* support http/0.9 (because
            # we'd have to use \r\n as a terminator, and it would just
            # yuck up a lot of stuff) it's very common for developers
            # to not want to type a version number when using telnet
            # to debug a server.
            close_it = 1

        outgoing_header = producers.simple_producer(self.build_reply_header())

        if close_it:
            self['Connection'] = 'close'

        if wrap_in_chunking:
            outgoing_producer = deferring_chunked_producer(
                    deferring_composite_producer(self.outgoing)
                    )
            # prepend the header
            outgoing_producer = deferring_composite_producer(
                [outgoing_header, outgoing_producer]
                )
        else:
            # prepend the header
            self.outgoing.insert(0, outgoing_header)
            outgoing_producer = deferring_composite_producer(self.outgoing)

        # hook logging into the output
        outgoing_producer = deferring_hooked_producer(outgoing_producer,
                                                      self.log)

        if globbing:
            outgoing_producer = deferring_globbing_producer(outgoing_producer)

        self.channel.push_with_producer(outgoing_producer)

        self.channel.current_request = None

        if close_it:
            self.channel.close_when_done()

    def log (self, bytes):
        """ We need to override this because UNIX domain sockets return
        an empty string for the addr rather than a (host, port) combination """
        if self.channel.addr:
            host = self.channel.addr[0]
            port = self.channel.addr[1]
        else:
            host = 'localhost'
            port = 0
        self.channel.server.logger.log (
                host,
                '%d - - [%s] "%s" %d %d\n' % (
                        port,
                        self.log_date_string (time.time()),
                        self.request,
                        self.reply_code,
                        bytes
                        )
                )

    def cgi_environment(self):
        env = {}

        # maps request some headers to environment variables.
        # (those that don't start with 'HTTP_')
        header2env= {'content-length'    : 'CONTENT_LENGTH',
                     'content-type'      : 'CONTENT_TYPE',
                     'connection'        : 'CONNECTION_TYPE'}

        workdir = os.getcwd()
        (path, params, query, fragment) = self.split_uri()

        if params:
            path = path + params # undo medusa bug!

        while path and path[0] == '/':
            path = path[1:]
        if '%' in path:
            path = http_server.unquote(path)
        if query:
            query = query[1:]

        server = self.channel.server
        env['REQUEST_METHOD'] = self.command.upper()
        env['SERVER_PORT'] = str(server.port)
        env['SERVER_NAME'] = server.server_name
        env['SERVER_SOFTWARE'] = server.SERVER_IDENT
        env['SERVER_PROTOCOL'] = "HTTP/" + self.version
        env['channel.creation_time'] = self.channel.creation_time
        env['SCRIPT_NAME'] = ''
        env['PATH_INFO'] = '/' + path
        env['PATH_TRANSLATED'] = os.path.normpath(os.path.join(
                workdir, env['PATH_INFO']))
        if query:
            env['QUERY_STRING'] = query
        env['GATEWAY_INTERFACE'] = 'CGI/1.1'
        if self.channel.addr:
            env['REMOTE_ADDR'] = self.channel.addr[0]
        else:
            env['REMOTE_ADDR'] = '127.0.0.1'

        for header in self.header:
            key,value=header.split(":",1)
            key=key.lower()
            value=value.strip()
            if key in header2env and value:
                env[header2env.get(key)]=value
            else:
                key='HTTP_%s' % ("_".join(key.split( "-"))).upper()
                if value and key not in env:
                    env[key]=value
        return env

    def get_server_url(self):
        """ Functionality that medusa's http request doesn't have; set an
        attribute named 'server_url' on the request based on the Host: header
        """
        default_port={'http': '80', 'https': '443'}
        environ = self.cgi_environment()
        if (environ.get('HTTPS') in ('on', 'ON') or
            environ.get('SERVER_PORT_SECURE') == "1"):
            # XXX this will currently never be true
            protocol = 'https'
        else:
            protocol = 'http'

        if 'HTTP_HOST' in environ:
            host = environ['HTTP_HOST'].strip()
            hostname, port = urllib.splitport(host)
        else:
            hostname = environ['SERVER_NAME'].strip()
            port = environ['SERVER_PORT']

        if port is None or default_port[protocol] == port:
            host = hostname
        else:
            host = hostname + ':' + port
        server_url = '%s://%s' % (protocol, host)
        if server_url[-1:]=='/':
            server_url=server_url[:-1]
        return server_url

class deferring_http_channel(http_server.http_channel):

    # use a 4096-byte buffer size instead of the default 65536-byte buffer in
    # order to spew tail -f output faster (speculative)
    ac_out_buffer_size = 4096

    delay = 0 # seconds
    last_writable_check = 0 # timestamp of last writable check; 0 if never

    def writable(self, now=None):
        if now is None:  # for unit tests
            now = time.time()

        if self.delay:
            # we called a deferred producer via this channel (see refill_buffer)
            elapsed = now - self.last_writable_check
            if (elapsed > self.delay) or (elapsed < 0):
                self.last_writable_check = now
                return True
            else:
                return False

        return http_server.http_channel.writable(self)

    def refill_buffer (self):
        """ Implement deferreds """
        while 1:
            if len(self.producer_fifo):
                p = self.producer_fifo.first()
                # a 'None' in the producer fifo is a sentinel,
                # telling us to close the channel.
                if p is None:
                    if not self.ac_out_buffer:
                        self.producer_fifo.pop()
                        self.close()
                    return
                elif isinstance(p, bytes):
                    self.producer_fifo.pop()
                    self.ac_out_buffer += p
                    return

                data = p.more()

                if data is NOT_DONE_YET:
                    self.delay = p.delay
                    return

                elif data:
                    self.ac_out_buffer = self.ac_out_buffer + data
                    self.delay = False
                    return
                else:
                    self.producer_fifo.pop()
            else:
                return

    def found_terminator (self):
        """ We only override this to use 'deferring_http_request' class
        instead of the normal http_request class; it sucks to need to override
        this """
        if self.current_request:
            self.current_request.found_terminator()
        else:
            # we convert the header to text to facilitate processing.
            # some of the underlying APIs (such as splitquery)
            # expect text rather than bytes.
            header = as_string(self.in_buffer)
            self.in_buffer = b''
            lines = header.split('\r\n')

            # --------------------------------------------------
            # crack the request header
            # --------------------------------------------------

            while lines and not lines[0]:
                # as per the suggestion of http-1.1 section 4.1, (and
                # Eric Parker <eparker@zyvex.com>), ignore a leading
                # blank lines (buggy browsers tack it onto the end of
                # POST requests)
                lines = lines[1:]

            if not lines:
                self.close_when_done()
                return

            request = lines[0]

            command, uri, version = http_server.crack_request (request)
            header = http_server.join_headers (lines[1:])

            # unquote path if necessary (thanks to Skip Montanaro for pointing
            # out that we must unquote in piecemeal fashion).
            rpath, rquery = http_server.splitquery(uri)
            if '%' in rpath:
                if rquery:
                    uri = http_server.unquote(rpath) + '?' + rquery
                else:
                    uri = http_server.unquote(rpath)

            r = deferring_http_request(self, request, command, uri, version,
                                       header)
            self.request_counter.increment()
            self.server.total_requests.increment()

            if command is None:
                self.log_info ('Bad HTTP request: %s' % repr(request), 'error')
                r.error (400)
                return

            # --------------------------------------------------
            # handler selection and dispatch
            # --------------------------------------------------
            for h in self.server.handlers:
                if h.match (r):
                    try:
                        self.current_request = r
                        # This isn't used anywhere.
                        # r.handler = h # CYCLE
                        h.handle_request (r)
                    except:
                        self.server.exceptions.increment()
                        (file, fun, line), t, v, tbinfo = \
                               asyncore.compact_traceback()
                        self.server.log_info(
                            'Server Error: %s, %s: file: %s line: %s' %
                            (t,v,file,line),
                            'error')
                        try:
                            r.error (500)
                        except:
                            pass
                    return

            # no handlers, so complain
            r.error (404)

class supervisor_http_server(http_server.http_server):
    channel_class = deferring_http_channel
    ip = None

    def prebind(self, sock, logger_object):
        """ Override __init__ to do logger setup earlier so it can
        go to our logger object instead of stdout """
        from supervisor.medusa import logger

        if not logger_object:
            logger_object = logger.file_logger(sys.stdout)

        logger_object = logger.unresolving_logger(logger_object)
        self.logger = logger_object

        asyncore.dispatcher.__init__ (self)
        self.set_socket(sock)

        self.handlers = []

        sock.setblocking(0)
        self.set_reuse_addr()

    def postbind(self):
        from supervisor.medusa.counter import counter
        from supervisor.medusa.http_server import VERSION_STRING

        self.listen(1024)

        self.total_clients = counter()
        self.total_requests = counter()
        self.exceptions = counter()
        self.bytes_out = counter()
        self.bytes_in  = counter()

        self.log_info (
                'Medusa (V%s) started at %s'
                '\n\tHostname: %s'
                '\n\tPort:%s'
                '\n' % (
                        VERSION_STRING,
                        time.ctime(time.time()),
                        self.server_name,
                        self.port,
                        )
                )

    def log_info(self, message, type='info'):
        ip = ''
        if getattr(self, 'ip', None) is not None:
            ip = self.ip
        self.logger.log(ip, message)

class supervisor_af_inet_http_server(supervisor_http_server):
    """ AF_INET version of supervisor HTTP server """

    def __init__(self, ip, port, logger_object):
        self.ip = ip
        self.port = port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.prebind(sock, logger_object)
        self.bind((ip, port))

        if not ip:
            self.log_info('Computing default hostname', 'warning')
            hostname = socket.gethostname()
            try:
                ip = socket.gethostbyname(hostname)
            except socket.error:
                raise ValueError(
                    'Could not determine IP address for hostname %s, '
                    'please try setting an explicit IP address in the "port" '
                    'setting of your [inet_http_server] section.  For example, '
                    'instead of "port = 9001", try "port = 127.0.0.1:9001."'
                    % hostname)
        try:
            self.server_name = socket.gethostbyaddr (ip)[0]
        except socket.error:
            self.log_info('Cannot do reverse lookup', 'warning')
            self.server_name = ip       # use the IP address as the "hostname"

        self.postbind()

class supervisor_af_unix_http_server(supervisor_http_server):
    """ AF_UNIX version of supervisor HTTP server """

    def __init__(self, socketname, sockchmod, sockchown, logger_object):
        self.ip = socketname
        self.port = socketname

        # XXX this is insecure.  We really should do something like
        # http://developer.apple.com/samplecode/CFLocalServer/listing6.html
        # (see also http://developer.apple.com/technotes/tn2005/tn2083.html#SECUNIXDOMAINSOCKETS)
        # but it would be very inconvenient for the user to need to get all
        # the directory setup right.

        tempname = "%s.%d" % (socketname, os.getpid())

        try:
            os.unlink(tempname)
        except OSError:
            pass

        while 1:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(tempname)
                os.chmod(tempname, sockchmod)
                try:
                    # hard link
                    os.link(tempname, socketname)
                except OSError:
                    # Lock contention, or stale socket.
                    used = self.checkused(socketname)
                    if used:
                        # cooperate with 'openhttpserver' in supervisord
                        raise socket.error(errno.EADDRINUSE)

                    # Stale socket -- delete, sleep, and try again.
                    msg = "Unlinking stale socket %s\n" % socketname
                    sys.stderr.write(msg)
                    try:
                        os.unlink(socketname)
                    except:
                        pass
                    sock.close()
                    time.sleep(.3)
                    continue
                else:
                    try:
                        os.chown(socketname, sockchown[0], sockchown[1])
                    except OSError as why:
                        if why.args[0] == errno.EPERM:
                            msg = ('Not permitted to chown %s to uid/gid %s; '
                                   'adjust "sockchown" value in config file or '
                                   'on command line to values that the '
                                   'current user (%s) can successfully chown')
                            raise ValueError(msg % (socketname,
                                                    repr(sockchown),
                                                    pwd.getpwuid(
                                                        os.geteuid())[0],
                                                    ),
                                             )
                        else:
                            raise
                    self.prebind(sock, logger_object)
                    break

            finally:
                try:
                    os.unlink(tempname)
                except OSError:
                    pass

        self.server_name = '<unix domain socket>'
        self.postbind()

    def checkused(self, socketname):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(socketname)
            s.send(as_bytes("GET / HTTP/1.0\r\n\r\n"))
            s.recv(1)
            s.close()
        except socket.error:
            return False
        else:
            return True

class tail_f_producer:
    def __init__(self, request, filename, head, is_html=False):
        self.request = weakref.ref(request)
        self.filename = filename
        self.delay = 0.1
        self.is_html = is_html

        self._open()
        sz = self._fsize()
        if sz >= head:
            self.sz = sz - head

    def __del__(self):
        self._close()

    def more(self):
        self._follow()
        try:
            newsz = self._fsize()
        except (OSError, ValueError):
            # file descriptor was closed
            return b''
        bytes_added = newsz - self.sz
        if bytes_added < 0:
            self.sz = 0
            return "==> File truncated <==\n" if not self.is_html else "<span class='log-warn'>==> File truncated &lt;==</span>\n"
        if bytes_added > 0:
            self.file.seek(-bytes_added, 2)
            data = self.file.read(bytes_added)
            self.sz = newsz
            
            if self.is_html and data:
                # HTML 转义，防止破坏 HTML 结构
                data = data.replace(b'&', b'&amp;').replace(b'<', b'&lt;').replace(b'>', b'&gt;')
                # 高亮日志级别
                data = self._highlight_log_levels(data)
            
            return data
        return NOT_DONE_YET

    def _highlight_log_levels(self, data):
        """高亮显示不同级别的日志"""
        import re
        
        # 将字节数据转换为字符串以便使用正则表达式
        if isinstance(data, bytes):
            data_str = data.decode('utf-8', errors='replace')
        else:
            data_str = data
        
        # 定义日志级别的正则表达式和对应的 CSS 类
        patterns = [
            (r'\b(ERROR|CRITICAL|FATAL)\b', 'log-error'),
            (r'\b(WARN|WARNING)\b', 'log-warn'),
            (r'\b(INFO|NOTICE)\b', 'log-info'),
            (r'\b(DEBUG|TRACE)\b', 'log-debug')
        ]
        
        # 为每个匹配项添加 span 标签
        for pattern, css_class in patterns:
            data_str = re.sub(pattern, r'<span class="%s">\1</span>' % css_class, data_str)
            
        return data_str.encode('utf-8')

    def _open(self):
        self.file = open(self.filename, 'rb')
        self.ino = os.fstat(self.file.fileno())[stat.ST_INO]
        self.sz = 0

    def _close(self):
        self.file.close()

    def _follow(self):
        try:
            ino = os.stat(self.filename)[stat.ST_INO]
        except (OSError, ValueError):
            # file was unlinked
            return
            
        if self.ino != ino: # log rotation occurred
            self._close()
            self._open()

    def _fsize(self):
        return os.fstat(self.file.fileno())[stat.ST_SIZE]

class logtail_handler:
    IDENT = 'Logtail HTTP Request Handler'
    path = '/logtail'

    def __init__(self, supervisord):
        self.supervisord = supervisord

    def match(self, request):
        return request.uri.startswith(self.path)

    def handle_request(self, request):
        if request.command != 'GET':
            request.error (400) # bad request
            return

        path, params, query, fragment = request.split_uri()

        if '%' in path:
            path = http_server.unquote(path)

        # strip off all leading slashes
        while path and path[0] == '/':
            path = path[1:]

        path, process_name_and_channel = path.split('/', 1)

        try:
            process_name, channel = process_name_and_channel.split('/', 1)
        except ValueError:
            # no channel specified, default channel to stdout
            process_name = process_name_and_channel
            channel = 'stdout'

        from supervisor.options import split_namespec
        group_name, process_name = split_namespec(process_name)

        group = self.supervisord.process_groups.get(group_name)
        if group is None:
            request.error(404) # not found
            return

        process = group.processes.get(process_name)
        if process is None:
            request.error(404) # not found
            return

        logfile = getattr(process.config, '%s_logfile' % channel, None)

        if logfile is None or not os.path.exists(logfile):
            # we return 404 because no logfile is a temporary condition.
            # if the process has never been started, no logfile will exist
            # on disk.  a logfile of None is also a temporary condition,
            # since the config file can be reloaded.
            request.error(404) # not found
            return

        # 获取请求的 format 参数，决定返回 HTML 还是纯文本
        is_html = True
        if query:
            import cgi
            parsed_query = cgi.parse_qs(query)
            is_html = parsed_query.get('format', ['html'])[0] != 'plain'

        if is_html:
            # 返回美化的 HTML 页面
            mtime = os.stat(logfile)[stat.ST_MTIME]
            request['Last-Modified'] = http_date.build_http_date(mtime)
            request['Content-Type'] = 'text/html;charset=utf-8'
            request['X-Accel-Buffering'] = 'no'

            # 创建进程的完整名称
            full_process_name = process_name
            if group_name != process_name:
                full_process_name = "%s:%s" % (group_name, process_name)

            # 构建 HTML 头部
            html_head = '''<!DOCTYPE html>
<html>
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
  <title>进程 %s 的日志</title>
  <style>
    body {
      font-family: 'Helvetica Neue', Arial, sans-serif;
      margin: 0;
      padding: 0;
      background-color: #f5f5f5;
      color: #333;
    }
    
    .log-container {
      max-width: 1200px;
      margin: 20px auto;
      background: #fff;
      border-radius: 6px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.1);
      overflow: hidden;
    }
    
    .log-header {
      background: #2c3e50;
      color: white;
      padding: 15px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    
    .log-header h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 500;
    }
    
    .log-controls {
      display: flex;
      gap: 10px;
    }
    
    .log-controls a button {
      background: #3498db;
      color: white;
      border: none;
      border-radius: 4px;
      padding: 8px 12px;
      cursor: pointer;
      font-size: 14px;
      transition: background 0.2s;
    }
    
    .log-controls a button:hover {
      background: #2980b9;
    }
    
    .log-content {
      position: relative;
      overflow: auto;
      max-height: 70vh;
      background: #282c34;
      color: #abb2bf;
      padding: 0;
      margin: 0;
    }
    
    .log-content pre {
      margin: 0;
      padding: 15px;
      font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, Courier, monospace;
      font-size: 13px;
      line-height: 1.5;
      tab-size: 4;
      white-space: pre-wrap;
    }
    
    .log-footer {
      display: flex;
      justify-content: space-between;
      padding: 10px 20px;
      background: #f8f9fa;
      border-top: 1px solid #e9ecef;
    }
    
    .log-footer a {
      color: #3498db;
      text-decoration: none;
    }
    
    .log-footer a:hover {
      text-decoration: underline;
    }
    
    /* 高亮特定日志级别 */
    .log-error { color: #e06c75; }
    .log-warn { color: #e5c07b; }
    .log-info { color: #61afef; }
    .log-debug { color: #98c379; }
  </style>
</head>
<body>
  <div class="log-container">
    <div class="log-header">
      <h1>进程 %s 的日志</h1>
      <div class="log-controls">
        <a href="%s"><button type="button">刷新</button></a>
      </div>
    </div>
    
    <div class="log-content">
      <pre id="log-pre">''' % (full_process_name, full_process_name, request.uri)

            html_foot = '''</pre>
    </div>
    
    <div class="log-footer">
      <a href="/index.html">返回首页</a>
      <div>Supervisor %s</div>
    </div>
  </div>
</body>
</html>''' % (supervisor.options.VERSION)

            # 发送带有美化页面的响应
            request.push(html_head)
            request.push(tail_f_producer(request, logfile, 1024, is_html=True))
            request.push(html_foot)
            request.done()
            
        else:
            # 原始的纯文本响应
            mtime = os.stat(logfile)[stat.ST_MTIME]
            request['Last-Modified'] = http_date.build_http_date(mtime)
            request['Content-Type'] = 'text/plain;charset=utf-8'
            request['X-Accel-Buffering'] = 'no'
            
            request.push(tail_f_producer(request, logfile, 1024, is_html=False))
            request.done()

class mainlogtail_handler:
    IDENT = 'Main Logtail HTTP Request Handler'
    path = '/mainlogtail'

    def __init__(self, supervisord):
        self.supervisord = supervisord

    def match(self, request):
        return request.uri.startswith(self.path)

    def handle_request(self, request):
        if request.command != 'GET':
            request.error (400) # bad request
            return

        logfile = self.supervisord.options.logfile

        if logfile is None or not os.path.exists(logfile):
            # we return 404 because no logfile is a temporary condition.
            # even if a log file of None is configured, the config file
            # may be reloaded, and the new config may have a logfile.
            request.error(404) # not found
            return

        mtime = os.stat(logfile)[stat.ST_MTIME]
        request['Last-Modified'] = http_date.build_http_date(mtime)
        request['Content-Type'] = 'text/plain;charset=utf-8'
        # the lack of a Content-Length header makes the outputter
        # send a 'Transfer-Encoding: chunked' response

        request.push(tail_f_producer(request, logfile, 1024))

        request.done()

def make_http_servers(options, supervisord):
    servers = []
    wrapper = LogWrapper(options.logger)

    for config in options.server_configs:
        family = config['family']

        if family == socket.AF_INET:
            host, port = config['host'], config['port']
            hs = supervisor_af_inet_http_server(host, port,
                                                logger_object=wrapper)
        elif family == socket.AF_UNIX:
            socketname = config['file']
            sockchmod = config['chmod']
            sockchown = config['chown']
            hs = supervisor_af_unix_http_server(socketname,sockchmod, sockchown,
                                                logger_object=wrapper)
        else:
            raise ValueError('Cannot determine socket type %r' % family)

        from supervisor.xmlrpc import supervisor_xmlrpc_handler
        from supervisor.xmlrpc import SystemNamespaceRPCInterface
        from supervisor.web import supervisor_ui_handler

        subinterfaces = []
        for name, factory, d in options.rpcinterface_factories:
            try:
                inst = factory(supervisord, **d)
            except:
                tb = traceback.format_exc()
                options.logger.warn(tb)
                raise ValueError('Could not make %s rpc interface' % name)
            subinterfaces.append((name, inst))
            options.logger.info('RPC interface %r initialized' % name)

        subinterfaces.append(('system',
                              SystemNamespaceRPCInterface(subinterfaces)))
        xmlrpchandler = supervisor_xmlrpc_handler(supervisord, subinterfaces)
        tailhandler = logtail_handler(supervisord)
        maintailhandler = mainlogtail_handler(supervisord)
        uihandler = supervisor_ui_handler(supervisord)
        here = os.path.abspath(os.path.dirname(__file__))
        templatedir = os.path.join(here, 'ui')
        filesystem = filesys.os_filesystem(templatedir)
        defaulthandler = default_handler.default_handler(filesystem)

        username = config['username']
        password = config['password']

        if username:
            # wrap the xmlrpc handler and tailhandler in an authentication
            # handler
            users = {username:password}
            xmlrpchandler = supervisor_auth_handler(users, xmlrpchandler)
            tailhandler = supervisor_auth_handler(users, tailhandler)
            maintailhandler = supervisor_auth_handler(users, maintailhandler)
            uihandler = supervisor_auth_handler(users, uihandler)
            defaulthandler = supervisor_auth_handler(users, defaulthandler)
        else:
            options.logger.critical(
                'Server %r running without any HTTP '
                'authentication checking' % config['section'])
        # defaulthandler must be consulted last as its match method matches
        # everything, so it's first here (indicating last checked)
        hs.install_handler(defaulthandler)
        hs.install_handler(uihandler)
        hs.install_handler(maintailhandler)
        hs.install_handler(tailhandler)
        hs.install_handler(xmlrpchandler) # last for speed (first checked)
        servers.append((config, hs))

    return servers

class LogWrapper:
    '''Receives log messages from the Medusa servers and forwards
    them to the Supervisor logger'''
    def __init__(self, logger):
        self.logger = logger

    def log(self, msg):
        '''Medusa servers call this method.  There is no log level so
        we have to sniff the message.  We want "Server Error" messages
        from medusa.http_server logged as errors at least.'''
        if msg.endswith('\n'):
            msg = msg[:-1]
        if 'error' in msg.lower():
            self.logger.error(msg)
        else:
            self.logger.trace(msg)

class encrypted_dictionary_authorizer:
    def __init__ (self, dict):
        self.dict = dict

    def authorize(self, auth_info):
        username, password = auth_info
        if username in self.dict:
            stored_password = self.dict[username]
            if stored_password.startswith('{SHA}'):
                password_hash = sha1(as_bytes(password)).hexdigest()
                return stored_password[5:] == password_hash
            else:
                return stored_password == password
        else:
            return False

class supervisor_auth_handler(auth_handler):
    def __init__(self, dict, handler, realm='default'):
        auth_handler.__init__(self, dict, handler, realm)
        # override the authorizer with one that knows about SHA hashes too
        self.authorizer = encrypted_dictionary_authorizer(dict)
