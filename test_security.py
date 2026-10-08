#!/usr/bin/env python3
"""Fault-injection regressions: never touches real nft/tc/systemd."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
import portbw as bw
from test_portbw import fake_snapshot

PORT=41001
REC={'port':PORT,'up':1250000,'down':2500000,'pending':False}

class SecurityTests(unittest.TestCase):
    def snapshot(self):return fake_snapshot()

    def test_nft_burst_tamper_is_stale(self):
        snap=self.snapshot();snap[3]['limit']['burst']=9999999999
        self.assertFalse(bw.nft_ok(PORT,REC,snap))

    def test_nft_udp_tamper_is_stale(self):
        snap=self.snapshot();snap[5]['rule']['expr'][0]['match']['left']['payload']['protocol']='udp'
        self.assertFalse(bw.nft_ok(PORT,REC,snap))

    def test_nft_foreign_accept_is_stale(self):
        snap=self.snapshot();snap.insert(3,{'rule':{'handle':777,'chain':'inbound','comment':'foreign','expr':[{'accept':None}]}})
        self.assertFalse(bw.nft_ok(PORT,REC,snap))

    def test_nft_extra_commented_unknown_rule_is_stale(self):
        snap=self.snapshot();snap.append({'rule':{'handle':777,'chain':'inbound','comment':'pbw-41001-up-not_a_rule'}})
        self.assertFalse(bw.nft_ok(PORT,REC,snap))

    def test_forged_other_port_comment_cannot_bypass(self):
        snap=self.snapshot()
        # Looks like owned port 41002, but actually ACCEPTs all traffic.
        snap.insert(3,{'rule':{'handle':998,'chain':'inbound','comment':'pbw-41002-up-drop',
                               'expr':[{'accept':None}]}})
        self.assertFalse(bw.nft_ok(PORT,REC,snap))

    def test_nft_bad_base_chain_refuses_mutation(self):
        snap=self.snapshot()
        snap[1]['chain']['prio']=-4
        with patch.object(bw,'nft_snapshot',return_value=snap),patch.object(bw,'run') as run:
            with self.assertRaises(bw.Error):bw.ensure_base()
        run.assert_not_called()

    def test_nft_missing_chain_is_repaired_not_replaced(self):
        missing=[{'table':{'family':'inet','name':bw.TABLE}},
                 {'chain':{'name':'inbound','type':'filter','hook':'input','policy':'accept','prio':-5}}]
        full=missing+[{'chain':{'name':'outbound','type':'filter','hook':'output','policy':'accept','prio':-5}}]
        with patch.object(bw,'nft_snapshot',side_effect=[missing,full]),patch.object(bw,'run',return_value='') as run:
            self.assertEqual(bw.ensure_base(),full)
        self.assertIn('add chain inet pbw_policy outbound',run.call_args.kwargs['input'])
        self.assertNotIn('delete',run.call_args.kwargs['input'])
        self.assertNotIn('flush',run.call_args.kwargs['input'])

    def test_nft_wrong_rule_order_is_stale(self):
        snap=self.snapshot()
        # Drop must remain before the counter for same port.
        snap[5],snap[6]=snap[6],snap[5]
        self.assertFalse(bw.nft_ok(PORT,REC,snap))

    def test_nft_limit_missing_burst_is_stale(self):
        snap=self.snapshot();del snap[3]['limit']['burst']
        self.assertFalse(bw.nft_ok(PORT,REC,snap))

    def test_legacy_handle_identical_match_never_adopted(self):
        foreign={'pref':PORT,'protocol':'ip','kind':'flower','options':{
                 'handle':PORT,'keys':{'ip_proto':'tcp','dst_port':PORT}}}
        with self.assertRaises(bw.Error):bw.tc_find('eth0',PORT,'up',4,[foreign])
        self.assertIsNone(bw.tc_find('eth0',PORT,'up',4,[foreign],strict=False))

    def test_tc_foreign_same_pref_does_not_get_deleted(self):
        legacy={'pref':PORT,'protocol':'ip','kind':'flower','options':{
                 'handle':PORT,'keys':{'ip_proto':'tcp','dst_port':PORT}}}
        with patch.object(bw,'tc_prepare'),patch.object(bw,'tc_filters',return_value=[legacy]),\
             patch.object(bw,'run') as run:
            with self.assertRaises(bw.Error):bw.apply_tc(PORT,REC,'eth0')
        run.assert_not_called()

    def test_tc_json_eth_type_is_legitimate_and_extra_key_is_foreign(self):
        exact={'pref':PORT,'protocol':'ip','kind':'flower','chain':0,'options':{
               'handle':bw.tc_handle(PORT),'keys':{'eth_type':'ipv4','ip_proto':'tcp','dst_port':PORT}}}
        self.assertEqual(bw.tc_find('eth0',PORT,'up',4,[exact]),exact)
        wrong=copy.deepcopy(exact);wrong['options']['keys']['eth_type']='ipv6'
        with self.assertRaises(bw.Error):bw.tc_find('eth0',PORT,'up',4,[wrong])
        wrong=copy.deepcopy(exact);wrong['options']['keys']['src_ip']='192.0.2.1'
        with self.assertRaises(bw.Error):bw.tc_find('eth0',PORT,'up',4,[wrong])

    def test_explicit_legacy_migration_succeeds_after_full_preflight(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(bw,'STATE',Path(tmp)):
            bw.write_json(bw.port_file(PORT),REC)
            def tc_rows(iface,tcdir):
                field='dst_port' if tcdir=='ingress' else 'src_port'
                return [{'pref':PORT,'protocol':proto,'kind':'flower','chain':0,'options':{
                        'handle':PORT,'keys':{'eth_type':'ipv4' if proto=='ip' else 'ipv6',
                                            'ip_proto':'tcp',field:PORT}}} for proto in ('ip','ipv6')]
            with patch.object(bw,'nft_ok',return_value=True),                 patch.object(bw,'tc_qdiscs',return_value=[{'kind':'clsact'}]),                 patch.object(bw,'tc_filters',side_effect=tc_rows),                 patch.object(bw,'tc_rate_is_ok',return_value=True),                 patch.object(bw,'apply_tc'),patch.object(bw,'tc_ok',return_value=True),                 patch.object(bw,'run',return_value='') as run:
                bw.migrate_legacy(PORT,{'iface':'eth0','tc_enabled':True})
            deleted=[call.args[0] for call in run.call_args_list if call.args[0][:3]==['tc','filter','del']]
            self.assertEqual(len(deleted),4)
            self.assertTrue(all(x[x.index('handle')+1]==f'0x{PORT:x}' for x in deleted))

    def test_tc_foreign_same_reserved_handle_wrong_match_rejected(self):
        foreign={'pref':PORT,'protocol':'ip','kind':'flower','options':{
                 'handle':bw.tc_handle(PORT),'keys':{'ip_proto':'tcp','dst_port':PORT,'src_ip':'192.0.2.1'}}}
        with self.assertRaises(bw.Error):bw.tc_find('eth0',PORT,'up',4,[foreign])

    def test_tc_incorrect_action_extra_match_is_stale(self):
        text=(f'filter protocol ip pref {PORT} flower chain 0 handle 0x{bw.tc_handle(PORT):x}\n'
              '  eth_type ipv4\n  ip_proto tcp\n  dst_port 41001\n'
              '  src_ip 192.0.2.5\n  skip_hw\n'
              '  action order 1:  police 0x1 rate 10Mbit burst 123Kb action ok\n')
        self.assertFalse(bw.tc_rate_is_ok({'kind':'flower'},1250000,'eth0','up',PORT,4,text))

    def test_tc_incorrect_action_alone_is_stale(self):
        text=(f'filter protocol ip pref {PORT} flower chain 0 handle 0x{bw.tc_handle(PORT):x}\n'
              '  eth_type ipv4\n  ip_proto tcp\n  dst_port 41001\n  skip_hw\n'
              '  action order 1:  police 0x1 rate 10Mbit burst 123Kb action ok\n')
        self.assertFalse(bw.tc_rate_is_ok({'kind':'flower'},1250000,'eth0','up',PORT,4,text))

    def test_tc_burst_mismatch_is_stale(self):
        text=(f'filter protocol ip pref {PORT} flower chain 0 handle 0x{bw.tc_handle(PORT):x}\n'
              '  eth_type ipv4\n  ip_proto tcp\n  dst_port 41001\n  skip_hw\n'
              '  action order 1:  police 0x1 rate 10Mbit burst 999Kb action drop\n')
        self.assertFalse(bw.tc_rate_is_ok({'kind':'flower'},1250000,'eth0','up',PORT,4,text))

    def test_tc_unknown_extra_flower_key_stale(self):
        text=(f'filter protocol ip pref {PORT} flower chain 0 handle 0x{bw.tc_handle(PORT):x}\n'
              '  eth_type ipv4\n  ip_proto tcp\n  dst_port 41001\n'
              '  ip_flags nofrag\n  skip_hw\n'
              '  action order 1:  police 0x1 rate 10Mbit burst 123Kb action drop\n')
        self.assertFalse(bw.tc_rate_is_ok({'kind':'flower'},1250000,'eth0','up',PORT,4,text))

    def test_tc_filter_command_error_not_empty(self):
        with patch.object(bw,'run',side_effect=bw.Error('tc exited 2')):
            with self.assertRaises(bw.Error):bw.tc_filters('eth0','ingress')

    def test_old_record_survives_without_listener(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(bw,'STATE',Path(tmp)):
            bw.write_json(bw.port_file(PORT),REC)
            with patch.object(bw,'listen_ports',return_value=set()):
                self.assertIn(PORT,bw.records())

    def test_legacy_migration_preflight_failure_never_deletes(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(bw,'STATE',Path(tmp)):
            bw.write_json(bw.port_file(PORT),REC)
            legacy={'pref':PORT,'protocol':'ip','kind':'flower','options':{
                    'handle':PORT,'keys':{'ip_proto':'tcp','dst_port':PORT}}}
            with patch.object(bw,'nft_ok',return_value=True),patch.object(bw,'tc_qdiscs',return_value=[{'kind':'clsact'}]),\
                 patch.object(bw,'tc_filters',return_value=[legacy]),\
                 patch.object(bw,'run',return_value='') as run:
                with self.assertRaises(bw.Error):bw.migrate_legacy(PORT,{'iface':'eth0','tc_enabled':True})
            self.assertFalse(any(x.args[0][:3]==['tc','filter','del'] for x in run.call_args_list))

    def test_install_rollback_enable_then_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf,units,state=Path(tmp)/'c',Path(tmp)/'u',Path(tmp)/'s';units.mkdir()
            args=type('A',(),{'iface':'eth0','nft_only':True})()
            calls=[]
            def fake_run(argv,**kw):
                calls.append(argv)
                if argv[:2]==['systemctl','is-enabled']:return None
                if argv[:2]==['systemctl','is-active']:return None
                if argv[:2]==['systemctl','start'] and argv[2]=='portbw-watch.timer':raise bw.Error('after enable')
                return ''
            with patch.object(bw,'CONF',conf),patch.object(bw,'UNITS',units),patch.object(bw,'STATE',state),\
                 patch.object(bw,'LOCK',Path(tmp)/'lock'),patch.object(bw.shutil,'which',return_value='/x'),\
                 patch.object(bw,'ensure_base'),patch.object(bw,'run',side_effect=fake_run),\
                 patch.object(bw.Path,'exists',lambda self:True if str(self)=='/run/systemd/system' else __import__('os').path.exists(self)):
                with self.assertRaises(bw.Error):bw.install(args)
            self.assertTrue(any(c[:2]==['systemctl','enable'] for c in calls))
            self.assertTrue(any(c[:2]==['systemctl','disable'] for c in calls))
            self.assertFalse((conf/'config.json').exists())
            self.assertFalse(list(units.iterdir()))

if __name__=='__main__':unittest.main()
