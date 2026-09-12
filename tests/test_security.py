import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock
from omarchy_voice.security import dispatch_literal_error, redact_text, child_env
from omarchy_voice.workspace_files import open_file, write_text
from omarchy_voice.task_worker import sandbox_command
from omarchy_voice.trace import Trace


class SecurityTests(unittest.TestCase):
    def test_lua_code_cannot_hide_inside_a_dispatcher(self):
        for value in [
            'hl.dsp.focus((function() os.execute("touch /tmp/probe") end)())',
            'hl.dsp.focus({workspace=os.getenv("HOME")})',
            'hl.dsp.focus({}); os.execute("true"); hl.dsp.focus({})',
            'hl.dsp.focus({workspace="2" .. "3"})',
            'hl.dsp.focus({workspace=unsafe})',
            'hl.dsp.focus({}); -- comment',
        ]:
            with self.subTest(value=value):self.assertIsNotNone(dispatch_literal_error(value))

    def test_literal_dispatchers_remain_available(self):
        for value in ['hl.dsp.focus({workspace="3"})', 'hl.dsp.layout("preselect r")',
                      "hl.dsp.cursor.move({x=-10.5,y=20})", 'hl.dsp.exit()',
                      'hl.dsp.test({nested={true,false,nil}, [1]="text"});',
                      'hl.dsp.test({text="os.execute(\\\"foo\\\")"})']:
            self.assertIsNone(dispatch_literal_error(value), value)

    def test_depth_and_size_limits(self):
        self.assertIsNotNone(dispatch_literal_error('hl.dsp.test('+'{'*30+'}'*30+')'))
        self.assertIsNotNone(dispatch_literal_error('hl.dsp.test("'+'x'*20000+'")'))

    def test_workspace_reads_and_writes_do_not_follow_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'workspace';root.mkdir();secret=base/'secret';secret.write_text('private')
            (root/'link').symlink_to(base,target_is_directory=True)
            for operation in [lambda: write_text(root,'link/secret','overwrite'),
                              lambda: open_file(root,'link/secret').__enter__()]:
                with self.assertRaises((OSError,ValueError)):operation()
            self.assertEqual(secret.read_text(),'private')
            write_text(root,'nested/value','safe')
            with open_file(root,'nested/value') as stream:self.assertEqual(stream.read(),b'safe')

    def test_special_files_and_parent_traversal_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkfifo(Path(tmp)/'pipe')
            with self.assertRaises(ValueError):
                with open_file(tmp,'pipe'):pass
            with self.assertRaises(ValueError):write_text(tmp,'../escape','bad')

    def test_device_root_is_never_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('omarchy_voice.task_worker.shutil.which',return_value='/usr/bin/bwrap'):
                argv=sandbox_command(Path(tmp),['true'])
            self.assertIn('--dev',argv)
            self.assertFalse(any(argv[i:i+3]==['--dev-bind','/dev','/dev'] for i in range(len(argv))))

    def test_credentials_redacted_in_plain_text_and_child_environment(self):
        secret='example-secret-value-12345'
        with mock.patch.dict(os.environ,{'OPENAI_API_KEY':secret,'NORMAL_ENV':'visible'}):
            self.assertEqual(redact_text('output '+secret),'output [redacted]')
            self.assertNotIn('OPENAI_API_KEY',child_env())
            self.assertEqual(child_env()['NORMAL_ENV'],'visible')
            self.assertEqual(Trace.clean({'output':secret}),{'output':'[redacted]'})

    def test_control_socket_recovers_from_invalid_utf8_and_idle_client(self):
        from omarchy_voice import session
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(session,'RUNTIME_DIR',Path(tmp)), mock.patch.object(session,'SOCKET_PATH',Path(tmp)/'control.sock'):
            server=session.ControlServer(lambda command:'ok:'+command);server.start()
            try:
                deadline=time.monotonic()+2
                while not session.SOCKET_PATH.exists() and time.monotonic()<deadline:time.sleep(.01)
                with socket.socket(socket.AF_UNIX) as bad:
                    while True:
                        try:
                            bad.connect(str(session.SOCKET_PATH));break
                        except ConnectionRefusedError:
                            if time.monotonic()>deadline:raise
                            time.sleep(.01)
                    bad.sendall(b'\xff');bad.shutdown(socket.SHUT_WR)
                with socket.socket(socket.AF_UNIX) as idle:
                    idle.connect(str(session.SOCKET_PATH))
                    with socket.socket(socket.AF_UNIX) as good:
                        good.settimeout(3);good.connect(str(session.SOCKET_PATH));good.sendall(b'ping')
                        self.assertEqual(good.recv(100),b'ok:ping')
            finally:server.stop()

    def test_installer_refuses_dangerous_prefixes(self):
        import subprocess
        script=str(Path('share/install-paths.sh').resolve())
        for prefix in ('/', str(Path.home()), str(Path.cwd())):
            result=subprocess.run(['bash','-c','source "$1"; validate_install_prefix "$2" "$3"',
                                   'check',script,prefix,str(Path.cwd())],capture_output=True)
            self.assertNotEqual(result.returncode,0)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);(path/'unrelated.txt').write_text('keep')
            result=subprocess.run(['bash','-c','source "$1"; validate_install_prefix "$2" "$3"',
                                   'check',script,tmp,str(Path.cwd())],capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual((path/'unrelated.txt').read_text(),'keep')
