#!/usr/bin/env python3
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
import portbw as bw


def fake_snapshot(p=41001,up=1250000,down=2500000):
    snap=[{'table':{'family':'inet','name':bw.TABLE}},
          {'chain':{'name':'inbound','type':'filter','hook':'input','policy':'accept','prio':-5}},
          {'chain':{'name':'outbound','type':'filter','hook':'output','policy':'accept','prio':-5}}]
    for d,b in [('up',up),('down',down)]:
        f='dport' if d=='up' else 'sport';chain=bw.CHAIN[d]
        snap += [{'limit':{'name':bw.limitname(d,p),'rate':b,'per':'second','unit':'bytes','inv':True,'burst':bw.burst_bytes(b)}},
                 {'counter':{'name':bw.countname(d,p),'bytes':0}}]
        snap.append({'rule':{'comment':bw.comment(d,p,'drop'),'handle':len(snap), 'chain':chain,'expr':[
                    {'match':{'op':'==','left':{'payload':{'protocol':'tcp','field':f}},'right':p}},
                    {'limit':bw.limitname(d,p)},{'drop':None}]}})
        snap.append({'rule':{'comment':bw.comment(d,p,'count'),'handle':len(snap),'chain':chain,'expr':[
                    {'match':{'op':'==','left':{'payload':{'protocol':'tcp','field':f}},'right':p}},
                    {'counter':bw.countname(d,p)}]}})
    return snap

class TestPortbw(unittest.TestCase):
    def test_mbps_decimal_and_zero(self):
        self.assertEqual(bw.rate_bytes('10'),1250000)
        self.assertEqual(bw.rate_bytes('2.5'),312500)
        self.assertEqual(bw.rate_mbps(312500),'2.5')
        self.assertEqual(bw.rate_bytes('0'),0)
        for x in ('-1','inf','NaN','999999','0.0001'):
            with self.assertRaises(bw.Error):bw.rate_bytes(x)

    def test_nft_sanity_detects_missing_limit_or_counter(self):
        good=fake_snapshot()
        with patch.object(bw,'nft_snapshot',return_value=good):
            self.assertTrue(bw.nft_ok(41001,{'up':1250000,'down':2500000}))
        bad=[item for item in good if item.get('counter',{}).get('name')!='pbw_cu_41001']
        with patch.object(bw,'nft_snapshot',return_value=bad):
            self.assertFalse(bw.nft_ok(41001,{'up':1250000,'down':2500000}))
        bad2=fake_snapshot();bad2[3]['limit']['rate']=9000000
        with patch.object(bw,'nft_snapshot',return_value=bad2):
            self.assertFalse(bw.nft_ok(41001,{'up':1250000,'down':2500000}))

    def test_nft_uses_two_shared_objects_not_per_connection(self):
        observed=[]
        base=fake_snapshot()
        with patch.object(bw,'ensure_base',return_value=base),patch.object(bw,'owned',return_value=([],[])),\
                patch.object(bw,'run',side_effect=lambda argv,**kw:observed.append((argv,kw)) or ''),\
                patch.object(bw,'nft_ok',return_value=True):
            bw.apply_nft(41001,{'up':1250000,'down':2500000})
        txt=observed[0][1]['input']
        self.assertIn('rate over 1250000 bytes/second',txt)
        self.assertIn('tcp dport 41001 limit name "pbw_u_41001" drop',txt)
        self.assertIn('tcp sport 41001 limit name "pbw_d_41001" drop',txt)
        self.assertEqual(txt.count('add limit'),2)
        self.assertNotIn('flush ruleset',txt)
        self.assertNotIn('delete table',txt)

    def test_pending_state_retained_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(bw,'STATE',Path(tmp)):
            with patch.object(bw,'apply',side_effect=bw.Error('tc error')):
                with self.assertRaises(bw.Error):bw.commit(41001,125000,250000,{'iface':'eth0','tc_enabled':True})
            rec=bw.read_json(bw.port_file(41001))
            self.assertTrue(rec['pending']);self.assertEqual(rec['down'],250000)

    def test_deletion_is_explicit_even_without_listener(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(bw,'STATE',Path(tmp)):
            rec={'port':41001,'up':1250000,'down':2500000,'pending':False}
            bw.write_json(bw.port_file(41001),rec)
            with patch.object(bw,'listen_ports',return_value=set()):
                self.assertIn(41001,bw.records())
            with patch.object(bw,'apply'):
                bw.commit(41001,0,0,{'iface':'eth0','tc_enabled':False},deleting=True)
            self.assertFalse(bw.port_file(41001).exists())

    def test_tc_uses_clsact_never_root(self):
        c=[]
        with patch.object(bw,'tc_qdiscs',return_value=[]),patch.object(bw,'run',side_effect=lambda x,**kw:c.append(x) or ''):
            bw.tc_prepare('eth0')
        self.assertEqual(c[0],['tc','qdisc','add','dev','eth0','clsact'])
        self.assertNotIn('root',c[0])
        with patch.object(bw,'tc_qdiscs',return_value=[{'kind':'ingress'}]):
            with self.assertRaises(bw.Error):bw.tc_prepare('eth0')

    def test_foreign_tc_filter_collision_rejected(self):
        row={'pref':41001,'protocol':'ip','kind':'flower','options':{
             'handle':41002,'keys':{'ip_proto':'tcp','dst_port':41001}}}
        with patch.object(bw,'tc_filters',return_value=[row]):
            with self.assertRaises(bw.Error):bw.tc_find('eth0',41001,'up',4)

    def test_tc_rate_is_read_from_real_filter_text_not_json(self):
        entry={'kind':'flower'}
        stdout=('filter protocol ip pref 41001 flower chain 0\n'
                'filter protocol ip pref 41001 flower chain 0 handle 0xb70a029\n'
                '  eth_type ipv4\n'
                 '  ip_proto tcp\n'
                 '  dst_port 41001\n'
                 '  skip_hw\n'
                '  action order 1:  police 0x1 rate 10Mbit burst 123Kb mtu 2Kb action drop\n')
        with patch.object(bw,'run',return_value=stdout):
            self.assertTrue(bw.tc_rate_is_ok(entry,1250000,'eth0','up',41001,4))
            self.assertFalse(bw.tc_rate_is_ok(entry,2500000,'eth0','up',41001,4))

    def test_install_units_keep_port_policies_no_node_hooks(self):
        src='\n'.join(bw.units().values())
        self.assertNotIn('vless',src)
        self.assertNotIn('socks5',src)
        self.assertIn('OnUnitInactiveSec=30s',src)
        self.assertIn('ExecStart=/usr/local/sbin/portbw repair',src)

    def test_nft_ok_uses_supplied_snapshot_without_new_nft_call(self):
        with patch.object(bw,'nft_snapshot',side_effect=AssertionError('no call')):
            self.assertTrue(bw.nft_ok(41001,{'up':1250000,'down':2500000},fake_snapshot()))

    def test_foreign_tc_filter_ignored_when_direction_unlimited(self):
        row={'pref':41001,'protocol':'ip','kind':'flower','options':{
             'handle':41002,'keys':{'ip_proto':'tcp','dst_port':41001}}}
        self.assertIsNone(bw.tc_find('eth0',41001,'up',4,[row],strict=False))
        with self.assertRaises(bw.Error):bw.tc_find('eth0',41001,'up',4,[row])

    def test_tc_ok_lists_each_direction_once(self):
        calls=[]
        def fake_run(argv,**kw):
            calls.append(argv)
            return ('filter protocol ip pref 41001 flower chain 0 handle 0xb70a029\n'
                    '  eth_type ipv4\n'
                     '  ip_proto tcp\n'
                     '  dst_port 41001\n'
                     '  skip_hw\n'
                     '  action order 1:  police 0x1 rate 10Mbit burst 123Kb action drop\n'
                    'filter protocol ipv6 pref 41001 flower chain 0 handle 0xb70a029\n'
                    '  eth_type ipv6\n'
                     '  ip_proto tcp\n'
                     '  dst_port 41001\n'
                     '  skip_hw\n'
                     '  action order 1:  police 0x1 rate 10Mbit burst 123Kb action drop\n')
        def rows(iface,d):
            if d=='egress':return []  # down==0: no filters expected
            return [{'pref':41001,'protocol':p,'kind':'flower','options':{'handle':bw.tc_handle(41001),
                     'keys':{'ip_proto':'tcp','dst_port' if d=='ingress' else 'src_port':41001}}} for p in ('ip','ipv6')]
        with patch.object(bw,'tc_qdiscs',return_value=[{'kind':'clsact'}]),patch.object(bw,'tc_filters',side_effect=rows),\
                patch.object(bw,'run',side_effect=fake_run):
            self.assertTrue(bw.tc_ok(41001,{'up':1250000,'down':0},'eth0'))
        self.assertEqual(len(calls),1)  # one `tc -s` for up; down==0 needs none

    def test_install_rolls_back_config_and_units_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf,units,state=Path(tmp)/'c',Path(tmp)/'u',Path(tmp)/'s';units.mkdir()
            args=type('A',(),{'iface':'eth0','nft_only':True})()
            with patch.object(bw,'CONF',conf),patch.object(bw,'UNITS',units),patch.object(bw,'STATE',state),\
                    patch.object(bw,'LOCK',Path(tmp)/'lock'),patch.object(bw.shutil,'which',return_value='/x'),\
                    patch.object(bw,'ensure_base'),patch.object(bw,'run',side_effect=lambda a,**k:
                        (_ for _ in ()).throw(bw.Error('boom')) if a[:2]==['systemctl','enable'] else ''),\
                    patch.object(bw.Path,'exists',lambda self:True if str(self)=='/run/systemd/system' else Path.stat and __import__('os').path.exists(self)):
                with self.assertRaises(bw.Error):bw.install(args)
            self.assertFalse((conf/'config.json').exists())
            self.assertEqual(list(units.iterdir()),[])

if __name__=='__main__':unittest.main()
