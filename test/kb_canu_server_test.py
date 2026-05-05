# -*- coding: utf-8 -*-
import os
import sys
import time
import unittest
from configparser import ConfigParser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from kb_canu.kb_canuImpl import kb_canu
from kb_canu.kb_canuServer import MethodContext
from installed_clients.authclient import KBaseAuth as _KBaseAuth
from installed_clients.WorkspaceClient import Workspace


class kb_canuTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        token = os.environ.get('KB_AUTH_TOKEN', None)
        config_file = os.environ.get('KB_DEPLOYMENT_CONFIG', None)
        cls.cfg = {}
        config = ConfigParser()
        config.read(config_file)
        for nameval in config.items('kb_canu'):
            cls.cfg[nameval[0]] = nameval[1]
        authServiceUrl = cls.cfg['auth-service-url']
        auth_client = _KBaseAuth(authServiceUrl)
        user_id = auth_client.get_user(token)
        cls.ctx = MethodContext(None)
        cls.ctx.update({'token': token,
                        'user_id': user_id,
                        'provenance': [
                            {'service': 'kb_canu',
                             'method': 'please_never_use_it_in_production',
                             'method_params': []
                             }],
                        'authenticated': 1})
        cls.wsURL = cls.cfg['workspace-url']
        cls.wsClient = Workspace(cls.wsURL)
        cls.serviceImpl = kb_canu(cls.cfg)
        cls.scratch = cls.cfg['scratch']
        cls.callback_url = os.environ['SDK_CALLBACK_URL']
        suffix = int(time.time() * 1000)
        cls.wsName = "test_kb_canu_" + str(suffix)
        ret = cls.wsClient.create_workspace({'workspace': cls.wsName})

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'wsName'):
            cls.wsClient.delete_workspace({'workspace': cls.wsName})
            print('Test workspace was deleted')

    def test_status(self):
        result = self.serviceImpl.status(self.ctx)
        self.assertEqual(result[0]['state'], 'OK')

    def test_run_canu_missing_params_raises(self):
        with self.assertRaises((ValueError, Exception)):
            self.serviceImpl.run_kb_canu(self.ctx, {
                'read_type': 'nanopore',
                'genome_size': '5m',
                'output_assembly_name': 'test_assembly',
                'workspace_name': self.wsName,
            })

    def test_run_canu_bad_read_type_raises(self):
        with self.assertRaises((ValueError, Exception)):
            self.serviceImpl.run_kb_canu(self.ctx, {
                'reads_ref': '1/2/3',
                'read_type': 'illumina',
                'genome_size': '5m',
                'output_assembly_name': 'test_assembly',
                'workspace_name': self.wsName,
            })

    def test_run_canu_bad_genome_size_raises(self):
        with self.assertRaises((ValueError, Exception)):
            self.serviceImpl.run_kb_canu(self.ctx, {
                'reads_ref': '1/2/3',
                'read_type': 'nanopore',
                'genome_size': 'not_a_size',
                'output_assembly_name': 'test_assembly',
                'workspace_name': self.wsName,
            })
