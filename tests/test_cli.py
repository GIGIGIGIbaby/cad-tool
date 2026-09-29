import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cad3 import cli
from cad3.project import Cad3Error


class CliTests(unittest.TestCase):
    def call(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv)
        return code, output.getvalue()

    def test_doctor_success_json(self):
        with patch.object(cli.runtime, 'doctor', return_value={'ok': True, 'version': ['1','1','3']}):
            code, text = self.call(['doctor'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)['command'], 'doctor')

    def test_protocol_failure_exit(self):
        with patch.object(cli.runtime, 'doctor', return_value={'ok': False, 'errors': ['missing completion']}):
            code, text = self.call(['doctor'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(text)['ok'])

    def test_user_error_is_json(self):
        with patch.object(cli.Project, 'load', side_effect=Cad3Error('not a CAD3 project')):
            code, text = self.call(['status'])
        self.assertEqual(code, 1)
        self.assertIn('not a CAD3', json.loads(text)['error']['message'])

    def test_code_view_is_explicit_text(self):
        with patch.object(cli.Project, 'load', return_value=object()), \
             patch.object(cli.runtime, 'inspect', return_value={'ok':True,'state':{'schema':'test'}}), \
             patch('cad3.observe.code_view', return_value='# READ ONLY\nBody = ...'):
            code, text = self.call(['inspect','--format','code'])
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith('# READ ONLY'))

    def test_invalid_or_duplicate_view_is_rejected(self):
        for views in ('bad', 'iso,iso', ',,'):
            with self.subTest(views=views), patch.object(cli.Project, 'load', return_value=object()), patch.object(cli.runtime,'render') as render:
                code, text = self.call(['render','--views',views])
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(text)['ok'])
                render.assert_not_called()

    def test_section_only_render_skips_default_views(self):
        with patch.object(cli.Project, 'load', return_value=object()), \
             patch.object(cli.runtime, 'render', return_value={'ok': True}) as render:
            code, _ = self.call(['render', '--section', 'y=12.5', '--section', 'z=0'])
        self.assertEqual(code, 0)
        kwargs = render.call_args.kwargs
        self.assertEqual(kwargs['views'], ())
        self.assertEqual(kwargs['sections'], ['y=12.5', 'z=0'])

    def test_section_with_explicit_views_keeps_them(self):
        with patch.object(cli.Project, 'load', return_value=object()), \
             patch.object(cli.runtime, 'render', return_value={'ok': True}) as render:
            self.call(['render', '--views', 'iso', '--section', 'x=0'])
        self.assertEqual(render.call_args.kwargs['views'], ('iso',))

    def test_render_size_is_bounded(self):
        with patch.object(cli.Project,'load',return_value=object()), patch.object(cli.runtime,'render') as render:
            code, _ = self.call(['render','--width','100000'])
        self.assertEqual(code,1)
        render.assert_not_called()

    def test_blank_commit_message(self):
        with patch.object(cli.Project,'load',return_value=object()), patch.object(cli.versions,'commit') as commit:
            code, _ = self.call(['commit','-m','   '])
        self.assertEqual(code,1)
        commit.assert_not_called()

    def test_nonfinite_result_cannot_escape_json(self):
        with patch.object(cli.runtime,'doctor',return_value={'ok':True,'bad':float('nan')}):
            code, text = self.call(['doctor'])
        self.assertEqual(code,1)
        self.assertFalse(json.loads(text)['ok'])

    def test_invalid_timeouts_fail_in_argparse(self):
        for value in ('0','-1','nan','inf'):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                cli.parser().parse_args(['doctor','--timeout',value])
            self.assertEqual(exc.exception.code,2)

    def test_absolute_outside_script_rejected(self):
        fake = SimpleNamespace(root=Path('/tmp/cad3-root'))
        with self.assertRaises(Cad3Error):
            cli._script(fake, '/tmp/other/script.py')


if __name__ == '__main__':
    unittest.main()
