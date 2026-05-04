# -*- coding: utf-8 -*-
"""
kb_canu_server_test.py

This is the test file that kb-sdk test discovers and runs inside the Docker
container via test/run_tests.sh.  It follows the KBase SDK standard pattern
for server-side integration tests.

Import structure matches what kb-sdk compile generates:
    lib/kb_canu/kb_canuImpl.py  →  imported as kb_canu (the class)
    lib/kb_canu/kb_canuServer.py → MethodContext
"""

import os
import sys
import unittest

# Add lib/ to path so imports work when run by nosetests inside Docker
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from kb_canu.kb_canuImpl import kb_canu
from kb_canu.kb_canuServer import MethodContext


class KbCanuServerTest(unittest.TestCase):
    """
    Integration tests for kb_canu that run inside the Docker container
    via kb-sdk test.  These tests require:
      - /kb/module/work/token  (written by kb-sdk test harness)
      - test/test.cfg          (copied from test/test.cfg.example and filled in)
      - A live KBase workspace reachable from within the container

    Unit tests (no credentials needed) live in:
      - test/test_kb_canu.py
      - test/test_reads_file_utils.py

    Run unit tests locally without Docker:
        PYTHONPATH=lib:test python -m pytest test/test_kb_canu.py
                                             test/test_reads_file_utils.py
                                             -v -k "not integration"
    """

    @classmethod
    def setUpClass(cls):
        token_file = '/kb/module/work/token'
        if not os.path.isfile(token_file):
            raise unittest.SkipTest(
                'No token file found at {}. '
                'Run "kb-sdk test" rather than invoking directly.'.format(token_file)
            )

        with open(token_file) as fh:
            token = fh.read().strip()

        # Read config from test/test.cfg
        from configparser import ConfigParser
        cfg_path = os.path.join(os.path.dirname(__file__), 'test.cfg')
        if not os.path.isfile(cfg_path):
            raise unittest.SkipTest(
                'test/test.cfg not found. '
                'Copy test/test.cfg.example to test/test.cfg and fill in values.'
            )

        config = ConfigParser()
        config.read(cfg_path)
        cls.cfg = dict(config.items('kb_canu'))
        cls.cfg['KB_AUTH_TOKEN'] = token
        cls.cfg['SDK_CALLBACK_URL'] = os.environ.get('SDK_CALLBACK_URL', '')

        # Resolve username from token
        try:
            from kb_canu.authclient import KBaseAuth
            auth_url = cls.cfg.get(
                'auth-service-url',
                'https://kbase.us/services/auth'
            )
            user_id = KBaseAuth(auth_url).get_user(token)
        except Exception:
            user_id = 'testuser'

        cls.ctx = MethodContext(None)
        cls.ctx.update({
            'token': token,
            'user_id': user_id,
            'authenticated': 1,
            'provenance': [{
                'service': 'kb_canu',
                'method': 'please_never_use_it_in_production',
                'method_params': [],
            }],
        })

        cls.ws_name = 'test_kb_canu_' + user_id
        cls.service = kb_canu(cls.cfg)

    # ------------------------------------------------------------------
    # Basic sanity tests — these run even without a workspace object
    # ------------------------------------------------------------------

    def test_status(self):
        """Module should report state=OK."""
        result = self.service.status(self.ctx)
        self.assertEqual(result[0]['state'], 'OK')
        self.assertIn('version', result[0])

    def test_run_canu_missing_params_raises(self):
        """Missing required params should raise ValueError, not crash the server."""
        with self.assertRaises((ValueError, Exception)):
            self.service.run_kb_canu(self.ctx, {
                # reads_ref intentionally missing
                'read_type': 'nanopore',
                'genome_size': '5m',
                'output_assembly_name': 'test_assembly',
                'workspace_name': self.ws_name,
            })

    def test_run_canu_bad_read_type_raises(self):
        """Unsupported read_type should raise ValueError."""
        with self.assertRaises((ValueError, Exception)):
            self.service.run_kb_canu(self.ctx, {
                'reads_ref': '1/2/3',
                'read_type': 'illumina',   # not supported
                'genome_size': '5m',
                'output_assembly_name': 'test_assembly',
                'workspace_name': self.ws_name,
            })

    def test_run_canu_bad_genome_size_raises(self):
        """Malformed genome_size should raise ValueError."""
        with self.assertRaises((ValueError, Exception)):
            self.service.run_kb_canu(self.ctx, {
                'reads_ref': '1/2/3',
                'read_type': 'nanopore',
                'genome_size': 'not_a_size',
                'output_assembly_name': 'test_assembly',
                'workspace_name': self.ws_name,
            })

    # ------------------------------------------------------------------
    # Full end-to-end integration tests
    # Set environment variables with real workspace read refs to enable:
    #   export CANU_TEST_NANOPORE_REF="ws_name/obj_name"
    #   export CANU_TEST_HIFI_REF="ws_name/obj_name"
    #   export CANU_TEST_CLR_REF="ws_name/obj_name"
    # ------------------------------------------------------------------

    def test_run_canu_nanopore_integration(self):
        reads_ref = os.environ.get('CANU_TEST_NANOPORE_REF')
        if not reads_ref:
            self.skipTest('CANU_TEST_NANOPORE_REF not set — skipping ONT integration test')

        result = self.service.run_kb_canu(self.ctx, {
            'workspace_name': self.ws_name,
            'reads_ref': reads_ref,
            'read_type': 'nanopore',
            'genome_size': '5m',
            'output_assembly_name': 'integration_canu_ont',
        })
        self.assertIn('report_ref', result[0])
        self.assertIn('report_name', result[0])

    def test_run_canu_pacbio_hifi_integration(self):
        reads_ref = os.environ.get('CANU_TEST_HIFI_REF')
        if not reads_ref:
            self.skipTest('CANU_TEST_HIFI_REF not set — skipping HiFi integration test')

        result = self.service.run_kb_canu(self.ctx, {
            'workspace_name': self.ws_name,
            'reads_ref': reads_ref,
            'read_type': 'pacbio-hifi',
            'genome_size': '5m',
            'output_assembly_name': 'integration_canu_hifi',
        })
        self.assertIn('report_ref', result[0])

    def test_run_canu_pacbio_clr_integration(self):
        reads_ref = os.environ.get('CANU_TEST_CLR_REF')
        if not reads_ref:
            self.skipTest('CANU_TEST_CLR_REF not set — skipping CLR integration test')

        result = self.service.run_kb_canu(self.ctx, {
            'workspace_name': self.ws_name,
            'reads_ref': reads_ref,
            'read_type': 'pacbio-raw',
            'genome_size': '5m',
            'output_assembly_name': 'integration_canu_clr',
        })
        self.assertIn('report_ref', result[0])


if __name__ == '__main__':
    unittest.main()
