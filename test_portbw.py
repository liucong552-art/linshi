#!/usr/bin/env python3
"""Offline regression tests. Run: python3 test_portbw.py [-v]
No root, nft/tc, network, or systemd required. Every kernel mutation is mocked.
"""
import argparse
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('portbw',HERE/'portbw.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
BOOT='00000000-0000-0000-0000-000000000001'
NEW_BOOT='00000000-0000-0000-0000-000000000002'


def options(**kw):
    args=dict(up='10',down='20',trigger='90',after='60s',auto_up='5',auto_down='8',hold='90s',cooldown='0s')
    args.update(kw)
    return p.auto_options(argparse.Namespace(**args))


def record(port=40001,**kw):
    a=options(**kw)
    r={'port':port,**a['base'],'auto':a,'pending':True,'deleting':False,
       'tc_prefs':{'4_tcp':port,'4_udp':65000-(port%100)*3,
                   '6_tcp':64999-(port%100)*3,'6_udp':64998-(port%100)*3}}
    p.reset_auto_boot(r,BOOT);r['pending']=False
    return r


def sample(t,b,gen=1,birth=0,width=0):
    return {'bytes':int(b),'start':float(t),'end':float(t+width),'birth_lo':float(birth),
            'birth_hi':float(birth+1),'generation':[gen]}


def rule(port,d,kind,handle):
    expr=[{'match':{'op':'==','left':{'meta':{'key':'l4proto'}},'right':{'set':['tcp','udp']}}},
          {'match':{'op':'==','left':{'payload':{'protocol':'th','field':'dport' if d=='up' else 'sport'}},'right':port}}]
    expr+=([{'limit':p.limitname(d,port)},{'drop':None}] if kind=='drop' else [{'counter':p.countname(d,port)}])
    return {'rule':{'family':'inet','table':p.TABLE,'chain':p.CHAIN[d],'handle':handle,
                    'comment':p.comment(d,port,kind),'expr':expr}}


def nft_fixture(port,r):
    result=[{'table':{'family':'inet','name':p.TABLE}}]
    result += [{'chain':{'family':'inet','table':p.TABLE,'name':chain,'type':'filter',
                        'hook':'input' if d=='up' else 'output','prio':-5,'policy':'accept'}} for d,chain in p.CHAIN.items()]
    h=1
    for d in p.CHAIN:
        if not r[d]:continue
        if d=='up':
            result.append({'limit':{'name':p.limitname(d,port),'rate':r[d],'rate_unit':'bytes',
                                   'burst':p.burst_bytes(r[d]),'burst_unit':'bytes','inv':True,'per':'second'}})
        result.append({'counter':{'name':p.countname(d,port),'bytes':0,'packets':0}})
        for kind in (('drop','count') if d=='up' else ('count',)):
            result.append(rule(port,d,kind,h));h+=1
    return result


def action_block(port,d,b,byte_count=0,age=100,bind=4):
    return (f'action order 1: police 0x{p.tc_police_index(port,d):x} rate {b*8}bit '
            f'burst {p.tc_burst_kb(b)*1024}b mtu 2Kb action drop/ok overhead 0b\n'
            f' index {p.tc_police_index(port,d)} ref {bind+1} bind {bind}\n'
            f' installed {age} sec used 0 sec\n skip_hw\n'
            f' Sent {byte_count} bytes 100 pkt (dropped 3, overlimits 3 requeues 0)\n')


def tc_fixture(port,r):
    rows={};texts={}
    for d,td in (('up','ingress'),('down','egress')):
        rows[td]=[];texts[td]=''
        if not r[d]:continue
        for fam in p.FAMILIES:
            for proto in p.TRANSPORTS:
                pref=p.tc_pref(port,fam,proto,r);field='dst_port' if d=='up' else 'src_port'
                rows[td].append({'protocol':p.tc_protocol(fam),'pref':pref,'kind':'flower','chain':0,
                                 'options':{'handle':hex(p.tc_handle(port)),
                                            'keys':{'ip_proto':proto,field:port,'eth_type':'ipv4' if fam==4 else 'ipv6'}}})
                texts[td]+=(f'filter protocol {p.tc_protocol(fam)} pref {pref} flower chain 0 handle {p.tc_handle(port):x}\n'
                             f' eth_type {"ipv4" if fam==4 else "ipv6"}\n ip_proto {proto}\n {field} {port}\n skip_hw\n not_in_hw\n'
                             +action_block(port,d,r[d]))
    return {'qdiscs':[{'kind':'clsact'},{'kind':'fq'}],'rows':rows,'text':texts}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.now=100.0
        for k,v in {'STATE':root/'state','CONF':root/'conf','LOCK':root/'run/lock','UNITS':root/'units','VOLATILE':root/'run/auto'}.items():
            patcher=patch.object(p,k,v);patcher.start();self.addCleanup(patcher.stop)
        for key,val in [('boot_id',lambda:BOOT),('boottime',lambda:self.now),
                        ('run',Mock(side_effect=AssertionError('Unexpected real kernel command'))),
                        ('nft_snapshot',Mock(side_effect=AssertionError('Unexpected real nft snapshot')))]:
            patcher=patch.object(p,key,val);patcher.start();self.addCleanup(patcher.stop)
        self.output=io.StringIO();self.redirect=contextlib.redirect_stdout(self.output);self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__,None,None,None)
        self.config={'iface':'eth0','tc_enabled':True,'version':2}
        p.write_json(p.CONF/'config.json',self.config)

    def tick(self,r,t,up,down):
        self.now=t
        p.advance_auto(r,t,{'up':sample(t,up),'down':sample(t,down)},{})
        if r.get('pending'):
            with patch.object(p,'apply'):
                p.finish_apply(r['port'],r,self.config)
        p.validate_record(r['port'],r)


class StateMachineTests(Base):
    def test_low_load_never_triggers(self):
        r=record()
        for t in range(100,701,30):self.tick(r,t,(t-100)*100000,(t-100)*100000)
        self.assertEqual(r['up'],1250000);self.assertEqual(r['runtime']['dirs']['up']['progress'],0)

    def test_short_peak_and_below_threshold_reset(self):
        r=record();self.tick(r,100,0,0);self.tick(r,130,37500000,0)
        self.assertEqual(r['runtime']['dirs']['up']['progress'],30)
        self.tick(r,160,38000000,0);self.assertEqual(r['runtime']['dirs']['up']['progress'],0)
        self.tick(r,190,75500000,0);self.assertEqual(r['up'],1250000)

    def test_up_down_and_simultaneous_are_independent(self):
        for hot in [('up',),('down',),('up','down')]:
            with self.subTest(hot=hot):
                r=record()
                for t in (100,130,160):self.tick(r,t,(t-100)*1250000 if 'up' in hot else 0,(t-100)*2500000 if 'down' in hot else 0)
                for d in p.CHAIN:
                    self.assertEqual(r[d],r['auto']['limited' if d in hot else 'base'][d])
                    if d in hot:self.assertEqual(r['runtime']['dirs'][d]['until'],250)

    def test_independent_expiry_and_no_early_release(self):
        r=record()
        for t in (100,130,160):self.tick(r,t,(t-100)*1250000,max(0,t-130)*2500000)
        self.tick(r,190,75000000,150000000)
        self.assertEqual(r['runtime']['dirs']['down']['until'],280)
        self.tick(r,220,75000000,150000000);self.assertEqual(r['up'],625000)
        self.tick(r,250,75000000,150000000)
        self.assertEqual(r['up'],1250000);self.assertEqual(r['down'],1000000)
        self.tick(r,280,75000000,150000000);self.assertEqual(r['down'],2500000)

    def test_trigger_again_after_recovery(self):
        r=record()
        for t in (100,130,160):self.tick(r,t,(t-100)*1250000,0)
        self.tick(r,250,75000000,0)
        for t in (280,310,340):self.tick(r,t,75000000+(t-280)*1250000,0)
        self.assertEqual(r['up'],625000);self.assertEqual(r['runtime']['dirs']['up']['until'],430)

    def test_cooldown_rebases_before_counting(self):
        r=record(cooldown='60s')
        for t in (100,130,160):self.tick(r,t,(t-100)*1250000,0)
        self.tick(r,250,187500000,0)
        self.assertEqual(r['runtime']['dirs']['up']['phase'],'cooldown')
        for t in (280,310,340):self.tick(r,t,(t-100)*1250000,0)
        self.assertEqual(r['up'],1250000)
        self.tick(r,370,337500000,0);self.assertEqual(r['up'],625000)

    def test_threshold_exact_and_jitter_no_forgiveness(self):
        r=record();total=0;self.tick(r,100,0,0)
        for t,rate in [(130,1125000),(160,1124999),(190,1125000)]:
            total+=rate*30;self.tick(r,t,total,0)
        self.assertEqual(r['up'],1250000)
        self.tick(r,220,total+1125000*30,0);self.assertEqual(r['up'],625000)

    def test_real_elapsed_not_assumed_30(self):
        r=record();self.tick(r,100,0,0);self.tick(r,140,40000000,0)
        self.assertEqual(r['runtime']['dirs']['up']['progress'],0)
        self.assertEqual(r['runtime']['dirs']['up']['mbps'],8)

    def test_systemd_delay_discards_long_gap(self):
        r=record();self.tick(r,100,0,0);self.tick(r,130,37500000,0)
        self.tick(r,210,137500000,0)
        self.assertEqual(r['up'],1250000);self.assertEqual(r['runtime']['dirs']['up']['progress'],0)

    def test_systemd_delay_does_not_lose_hold_deadline(self):
        r=record()
        for t in (100,130,160):self.tick(r,t,(t-100)*1250000,0)
        self.tick(r,1000,75000000,0);self.assertEqual(r['up'],1250000)

    def test_reset_wrap_generation_and_recreated_larger_counter(self):
        for change in [dict(b=1),dict(b=2**64-1,gen=2),dict(b=999999999,birth=145)]:
            with self.subTest(change=change):
                r=record();s=r['runtime']['dirs']['up']
                p.observe(s,sample(100,0),1250000,r['auto'])
                p.observe(s,sample(130,37500000),1250000,r['auto'])
                p.observe(s,sample(160,**change),1250000,r['auto'])
                self.assertEqual(s['phase'],'monitor');self.assertEqual(s['progress'],0)

    def test_sampling_failure_breaks_progress_not_hold(self):
        r=record();self.tick(r,100,0,0);self.tick(r,130,37500000,0)
        p.advance_auto(r,160,{}, {'up':'failure','down':'failure'})
        self.assertEqual(r['runtime']['dirs']['up']['progress'],0)
        for t in (190,220,250):self.tick(r,t,(t-100)*1250000,0)
        p.advance_auto(r,280,{},{});self.assertEqual(r['up'],625000)
        self.now=340;p.advance_auto(r,340,{},{});
        with patch.object(p,'apply'):p.finish_apply(40001,r,self.config)
        self.assertEqual(r['up'],1250000)

    def test_clock_reversal_does_not_restore(self):
        r=record()
        for t in (100,130,160):self.tick(r,t,(t-100)*1250000,0)
        with self.assertRaises(p.Error):p.advance_auto(r,90,{}, {})
        self.assertEqual(r['up'],625000)

    def test_read_uncertainty_prevents_threshold_false_positive(self):
        r=record();s=r['runtime']['dirs']['up']
        p.observe(s,sample(100,0,width=1),1250000,r['auto'])
        p.observe(s,sample(130,33750000,width=1),1250000,r['auto'])
        self.assertEqual(s['progress'],0);self.assertLess(s['lower_mbps'],9)


class PersistenceAndCLITests(Base):
    def test_old_static_record_loads_unchanged(self):
        old={'port':40001,'up':1250000,'down':2500000,'pending':False}
        p.write_json(p.port_file(40001),old);self.assertEqual(p.records()[40001],old)

    def test_invalid_parameters(self):
        cases=[{'trigger':v} for v in ('0','101','NaN','Infinity','-1','90.001')]
        cases += [{k:v} for k in ('up','down','auto_up','auto_down') for v in ('0','-1','NaN','Infinity','bad','0.001')]
        cases += [{'auto_up':'10'},{'auto_down':'30'},{'after':'30s'},{'after':'1.5m'},
                  {'hold':'0s'},{'hold':'10'},{'cooldown':'-1s'},{'after':'31d'}]
        for kw in cases:
            with self.subTest(kw=kw),self.assertRaises(p.Error):options(**kw)
        self.assertEqual(options(trigger='90.25')['trigger_bp'],9025)

    def test_corrupt_record_never_falls_back(self):
        for mutate in [lambda r:r.update(up=True),lambda r:r.update(runtime={}),
                       lambda r:r['auto'].update(trigger_bp=float('nan')),
                       lambda r:r['runtime']['dirs']['up'].update(phase='hold'),
                       lambda r:r['runtime']['dirs']['up'].update(last={'bytes':0}),
                       lambda r:r.update(up=0),lambda r:r['auto'].pop('base')]:
            r=record();mutate(r);p.write_json(p.port_file(40001),r)
            with self.assertRaises(p.Error):p.records()
        p.port_file(40001).write_text('{broken')
        with self.assertRaises(p.Error):p.records()
        p.write_json(p.CONF/'config.json',[])
        with self.assertRaises(p.Error):p.cfg()

    def test_manual_up_uses_other_base_and_turns_auto_off(self):
        r=record()
        for t in (100,130,160):self.tick(r,t,(t-100)*1250000,(t-100)*2500000)
        p.write_json(p.port_file(40001),r)
        with patch.object(p,'apply'):
            p.operate(argparse.Namespace(action='up',port='40001',up_mbit='7'))
        new=p.records()[40001]
        self.assertNotIn('auto',new);self.assertEqual((new['up'],new['down']),(875000,2500000))

    def test_auto_off_restores_base_and_deletion_removes_all_state(self):
        r=record();r['up']=r['auto']['limited']['up'];r['runtime']['dirs']['up'].update(phase='hold',until=200)
        p.write_json(p.port_file(40001),r)
        with patch.object(p,'apply') as apply:
            p.auto_operate(argparse.Namespace(auto_action='off',port='40001'),self.config)
            new=p.records()[40001];self.assertNotIn('auto',new);self.assertEqual(new['up'],1250000)
            p.operate(argparse.Namespace(action='del',port='40001'))
            self.assertFalse(p.port_file(40001).exists())
            self.assertTrue(apply.call_args.args[1]['deleting'])

    def test_failed_commit_persists_pending_intent(self):
        p.write_json(p.port_file(40001),record())
        with patch.object(p,'apply',side_effect=p.Error('kernel failed')),self.assertRaises(p.Error):
            p.commit(40001,625000,1000000,self.config)
        r=p.records()[40001];self.assertTrue(r['pending']);self.assertNotIn('auto',r)
        with patch.object(p,'apply'):p.finish_apply(40001,r,self.config)
        self.assertFalse(p.records()[40001]['pending'])

    def test_crash_after_intent_before_kernel_and_after_kernel_before_ack(self):
        for kernel_already_applied in (False,True):
            r=record();r['up']=625000;r['runtime']['dirs']['up'].update(phase='entering');r['pending']=True
            p.write_json(p.port_file(40001),r);self.now=500
            kernel={'up':625000 if kernel_already_applied else 1250000,'down':2500000}
            def replay(port,rec,config):
                kernel.update({d:rec[d] for d in p.CHAIN})
            with patch.object(p,'apply',side_effect=replay) as apply:
                p.finish_apply(40001,p.records()[40001],self.config)
            self.assertEqual(kernel,{'up':625000,'down':2500000})
            saved=p.records()[40001]
            self.assertEqual(saved['runtime']['dirs']['up']['until'],590)
            self.assertEqual(saved['up'],625000);apply.assert_called_once()

    def test_reboot_resets_only_auto_static_untouched(self):
        r=record();r['up']=625000;r['runtime']['dirs']['up'].update(phase='hold',until=1000)
        p.write_json(p.port_file(40001),r)
        with patch.object(p,'boot_id',return_value=NEW_BOOT),patch.object(p,'ensure_base',return_value=[]),\
             patch.object(p,'tc_snapshot',return_value={}),patch.object(p,'apply'):
            p.reconcile(self.config)
        new=p.records()[40001];self.assertEqual(new['up'],1250000)
        self.assertEqual(new['runtime']['boot_id'],NEW_BOOT)
        self.assertIsNone(new['runtime']['dirs']['up']['last'])

    def test_auto_set_upgrades_static_preserves_prefs_and_resets_old_hold(self):
        old=record();prefs=old['tc_prefs'];old.pop('auto');old.pop('runtime')
        p.write_json(p.port_file(40001),old)
        args=argparse.Namespace(auto_action='set',port='40001',up='10',down='20',trigger='90',
                                after='10m',auto_up='5',auto_down='8',hold='30m',cooldown='60s')
        with patch.object(p,'apply'):
            p.auto_operate(args,self.config)
            upgraded=p.records()[40001];self.assertEqual(upgraded['tc_prefs'],prefs)
            upgraded['up']=625000;upgraded['runtime']['dirs']['up'].update(phase='hold',until=900)
            p.write_json(p.port_file(40001),upgraded)
            p.auto_operate(args,self.config)
        new=p.records()[40001];self.assertEqual(new['up'],1250000)
        self.assertEqual(new['runtime']['dirs']['up']['phase'],'monitor')

    def test_failed_delete_tombstone_is_replayable(self):
        p.write_json(p.port_file(40001),record())
        with patch.object(p,'apply',side_effect=p.Error('delete failed')),self.assertRaises(p.Error):
            p.commit(40001,0,0,self.config,deleting=True)
        tomb=p.records()[40001];self.assertTrue(tomb['deleting']);self.assertTrue(tomb['pending'])
        self.assertNotIn('auto',tomb)
        with patch.object(p,'apply'):p.finish_apply(40001,tomb,self.config)
        self.assertFalse(p.port_file(40001).exists())

    def test_nft_only_auto_and_download_refused_before_writing(self):
        c=dict(self.config,tc_enabled=False)
        with self.assertRaises(p.Error):p.commit(40001,1250000,2500000,c)
        self.assertFalse(p.port_file(40001).exists())
        args=argparse.Namespace(auto_action='set',port='40001')
        with self.assertRaises(p.Error):p.auto_operate(args,c)

    def test_status_does_not_advance_state(self):
        r=record();p.write_json(p.port_file(40001),r);before=p.port_file(40001).read_bytes()
        with patch.object(p,'nft_snapshot',return_value=[]),patch.object(p,'tc_snapshot',return_value={}),\
             patch.object(p,'audit_one',return_value=('OK',True,True)):
            p.auto_operate(argparse.Namespace(auto_action='status',port='40001'),self.config)
        self.assertEqual(before,p.port_file(40001).read_bytes())
        self.assertIn('采样',self.output.getvalue())


class KernelContractTests(Base):
    def test_nft_shared_protocols_and_exact_order(self):
        r=record();snap=nft_fixture(40001,r);self.assertTrue(p.nft_ok(40001,r,snap))
        self.assertEqual(len([x for x in snap if 'limit' in x]),1)
        self.assertEqual(len([x for x in snap if 'counter' in x]),2)
        bad=copy.deepcopy(snap)
        rows=[x for x in bad if 'rule' in x]
        rows[0]['rule']['expr'][0]['match']['right']={'set':['tcp']}
        self.assertFalse(p.nft_ok(40001,r,bad))
        bad=copy.deepcopy(snap);i=[i for i,x in enumerate(bad) if 'rule' in x]
        bad[i[0]],bad[i[1]]=bad[i[1]],bad[i[0]]
        self.assertFalse(p.nft_ok(40001,r,bad))

    def test_dynamic_audit_uses_effective_rate(self):
        r=record();r['up']=625000;r['runtime']['dirs']['up'].update(phase='hold',until=200)
        self.assertEqual(p.audit_one(40001,r,self.config,nft_fixture(40001,r),tc_fixture(40001,r))[0],'OK')
        base=copy.deepcopy(r);base['up']=1250000
        self.assertEqual(p.audit_one(40001,r,self.config,nft_fixture(40001,base),tc_fixture(40001,base))[0],'STALE')

    def test_tc_four_filters_share_one_action_each_direction(self):
        r=record();snap=tc_fixture(40001,r)
        self.assertTrue(p.tc_ok(40001,r,'eth0',snap))
        for d in ('ingress','egress'):self.assertEqual(len(snap['rows'][d]),4)
        bad=copy.deepcopy(snap)
        bad['text']['ingress']=bad['text']['ingress'].replace(f'police 0x{p.tc_police_index(40001,"up"):x}', 'police 0x999',1)
        self.assertFalse(p.tc_ok(40001,r,'eth0',bad))

    def test_global_policer_bytes_not_summed_four_times(self):
        r=record();self.now=100
        output='\n'.join(action_block(40001,d,r[d],123456) for d in p.CHAIN)
        with patch.object(p,'run',return_value=output) as run:
            snapshot=p.traffic_snapshot();run.assert_called_once()
        value=p.traffic_sample(40001,'up',r,snapshot,nft_fixture(40001,r))
        self.assertEqual(value['bytes'],123456)

    def test_slow_or_failed_global_dump_rejected(self):
        for times in ([100,103],[100,99]):
            with patch.object(p,'boottime',side_effect=times),patch.object(p,'run',return_value=''),self.assertRaises(p.Error):
                p.traffic_snapshot()
        with patch.object(p,'run',side_effect=p.Error('tc failed')),self.assertRaises(p.Error):p.traffic_snapshot()

    def test_invalid_stats_fail_closed(self):
        r=record();block=action_block(40001,'up',r['up'])
        for invalid in [block.replace('Sent','Missing'),block.replace('installed','missing'),
                        block.replace('bind 4','bind 5'),block.replace('skip_hw','skip_sw')]:
            snap={'start':100,'end':100,'blocks':{p.tc_police_index(40001,'up'):invalid}}
            with self.assertRaises(p.Error):p.traffic_sample(40001,'up',r,snap,nft_fixture(40001,r))
        with self.assertRaises(p.Error):p.police_blocks(block+block)

    def test_nft_rate_update_does_not_rebuild_other_direction(self):
        r=record();snap=nft_fixture(40001,r);target=dict(r,up=625000)
        with patch.object(p,'ensure_base',return_value=snap),patch.object(p,'nft_snapshot',return_value=nft_fixture(40001,target)),\
             patch.object(p,'run',return_value='') as run:
            p.apply_nft(40001,target)
        script=run.call_args.kwargs['input']
        self.assertIn('pbw_u_40001',script);self.assertNotIn('outbound',script);self.assertNotIn('pbw_cd_40001',script)

    def test_live_rate_update_replaces_action_without_filter_deletion(self):
        old=record();snap=tc_fixture(40001,old);new=dict(old,up=625000)
        calls=[]
        def run(cmd,**kw):
            calls.append(cmd)
            if cmd[:4]==['tc','-s','filter','show']:return snap['text'][cmd[-1]]
            return ''
        with patch.object(p,'tc_prepare'),patch.object(p,'tc_filters',side_effect=lambda i,d:snap['rows'][d]),\
             patch.object(p,'tc_action_block',return_value=action_block(40001,'up',old['up'])),patch.object(p,'run',side_effect=run):
            p.apply_tc(40001,new,'eth0')
        changes=[cmd for cmd in calls if 'replace' in cmd]
        self.assertEqual(len(changes),1);self.assertIn(str(p.tc_police_index(40001,'up')),changes[0])
        self.assertFalse(any('del' in cmd or 'delete' in cmd for cmd in calls))

    def test_foreign_binding_prevents_action_update(self):
        old=record();snap=tc_fixture(40001,old);new=dict(old,up=625000)
        with patch.object(p,'tc_prepare'),patch.object(p,'tc_filters',side_effect=lambda i,d:snap['rows'][d]),\
             patch.object(p,'tc_action_block',return_value=action_block(40001,'up',old['up'],bind=5)),\
             patch.object(p,'run',side_effect=lambda cmd,**k:snap['text'][cmd[-1]]) as run:
            with self.assertRaises(p.Error):p.apply_tc(40001,new,'eth0')
        self.assertFalse(any('replace' in c.args[0] or 'del' in c.args[0] for c in run.call_args_list))

    def test_foreign_nft_rule_refused(self):
        r=record();snap=nft_fixture(40001,r);snap.append({'rule':{'chain':'inbound','handle':99,'comment':'foreign','expr':[{'accept':None}]}})
        with self.assertRaises(p.Error):p.validate_base(snap)
        with patch.object(p,'ensure_base',side_effect=p.Error('foreign')):
            with self.assertRaises(p.Error):p.apply_nft(40001,r)
        p.run.assert_not_called()

    def test_no_root_qdisc_or_global_flush_commands(self):
        r=record()
        with patch.object(p,'tc_qdiscs',return_value=[{'kind':'fq'}]),patch.object(p,'run',return_value='') as run:
            p.tc_prepare('eth0')
        run.assert_called_once_with(['tc','qdisc','add','dev','eth0','clsact'])
        with patch.object(p,'tc_qdiscs',return_value=[{'kind':'ingress'}]):
            with self.assertRaises(p.Error):p.tc_prepare('eth0')


class WatchIntegrationTests(Base):
    def setup_kernel(self,rows):
        for port,r in rows.items():p.write_json(p.port_file(port),r)
        state={port:copy.deepcopy(r) for port,r in rows.items()}
        def apply(port,rec,c):state[port]=copy.deepcopy(rec)
        def healthy(port,rec,*a,**k):return all(state[port][d]==rec[d] for d in p.CHAIN)
        for name,obj in [('apply',Mock(side_effect=apply)),('nft_ok',Mock(side_effect=healthy)),
                         ('tc_ok',Mock(side_effect=healthy)),('ensure_base',Mock(return_value=[])),
                         ('tc_snapshot',Mock(return_value={}))]:
            pa=patch.object(p,name,obj);pa.start();self.addCleanup(pa.stop)
        return state

    def test_many_ports_one_snapshot_and_separate_policies(self):
        rows={port:record(port) for port in range(40001,40011)};self.setup_kernel(rows)
        def traffic_sample(port,d,r,ss,ni):return sample(self.now,(self.now-100)*(1250000 if port==40001 and d=='up' else 0))
        with patch.object(p,'traffic_snapshot',return_value={}) as snapshot,patch.object(p,'traffic_sample',side_effect=traffic_sample):
            for t in (100,130,160):self.now=t;p.reconcile(self.config,monitor=True)
            self.assertEqual(snapshot.call_count,3)
            self.assertEqual(p.tc_snapshot.call_count,3)
        saved=p.records();self.assertEqual(saved[40001]['up'],625000)
        self.assertTrue(all(saved[i]['up']==1250000 for i in range(40002,40011)))

    def test_global_stats_failure_does_not_block_expired_hold(self):
        r=record();r['up']=625000;r['runtime']['dirs']['up'].update(phase='hold',until=100)
        self.setup_kernel({40001:r});self.now=110
        with patch.object(p,'traffic_snapshot',side_effect=p.Error('stats failed')),self.assertRaises(p.Error):
            p.reconcile(self.config,monitor=True)
        self.assertEqual(p.records()[40001]['up'],1250000)

    def test_rule_snapshot_failure_resets_progress_persists_hold(self):
        r=record();self.tick(r,100,0,0);self.tick(r,130,37500000,0)
        p.write_json(p.port_file(40001),r)
        with patch.object(p,'ensure_base',side_effect=p.Error('nft failed')),self.assertRaises(p.Error):p.reconcile(self.config,True)
        self.assertEqual(p.records()[40001]['runtime']['dirs']['up']['progress'],0)

    def test_partial_kernel_failure_retries_pending_next_watch(self):
        rows={40001:record()};state=self.setup_kernel(rows)
        def get_sample(port,d,r,ss,ni):return sample(self.now,(self.now-100)*(1250000 if d=='up' else 0))
        with patch.object(p,'traffic_snapshot',return_value={}),patch.object(p,'traffic_sample',side_effect=get_sample):
            for t in (100,130):self.now=t;p.reconcile(self.config,True)
            self.now=160
            with patch.object(p,'apply',side_effect=p.Error('tc failed')),self.assertRaises(p.Error):p.reconcile(self.config,True)
            saved=p.records()[40001];self.assertTrue(saved['pending']);self.assertEqual(saved['runtime']['dirs']['up']['phase'],'entering')
            self.now=200;p.reconcile(self.config,True)
        saved=p.records()[40001];self.assertFalse(saved['pending']);self.assertEqual(saved['runtime']['dirs']['up']['until'],290)


class InstallerTests(Base):
    def test_python_install_unit_config_rollback(self):
        old=dict(self.config,version=1);p.write_json(p.CONF/'config.json',old)
        for name in p.units():p.write_atomic(p.UNITS/name,b'old unit\n')
        original_exists=Path.exists
        def exists(path):return True if str(path)=='/run/systemd/system' else original_exists(path)
        calls=[]
        def run(cmd,**kw):
            calls.append(cmd)
            if cmd==['systemctl','start','portbw-watch.timer'] and kw.get('check',True):raise p.Error('start failed')
            if 'is-enabled' in cmd:return 'enabled\n'
            if 'is-active' in cmd:return 'inactive\n'
            return ''
        with patch.object(Path,'exists',exists),patch.object(p.shutil,'which',return_value='/mock'),\
             patch.object(p,'run',side_effect=run),patch.object(p,'tc_qdiscs',return_value=[]),\
             patch.object(p,'ensure_base',return_value=[]),patch.object(p,'tc_prepare'),self.assertRaises(p.Error):
            p.install(argparse.Namespace(iface='eth0',nft_only=False))
        self.assertEqual(p.cfg(),old)
        for name in p.units():self.assertEqual((p.UNITS/name).read_bytes(),b'old unit\n')
        self.assertIn(['systemctl','daemon-reload'],calls)

    def test_shell_outer_rollback_after_post_install_audit_failure(self):
        root=Path(self.temp.name)/'shell';root.mkdir()
        source=(HERE/'portbw-install.sh').read_text()
        body=source[source.index('install_files() ('):source.index('\ninstall_files  #')]
        for prefix in ('/usr/local','/etc/portbw','/etc/systemd/system','/run/portbw','/var/tmp'):
            body=body.replace(prefix,str(root)+prefix)
        fake=root/'mockpython'
        fake.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n'
                        f'root=Path({str(root)!r})\n'
                        'if "audit" in sys.argv: sys.exit(1)\n'
                        'if "install" in sys.argv:\n'
                        ' (root/"etc/portbw/config.json").write_text("changed config")\n'
                        ' (root/"etc/systemd/system/portbw-watch.timer").write_text("changed unit")\n')
        fake.chmod(0o755);body=body.replace('/usr/bin/python3',str(fake))
        tracked=['usr/local/lib/portbw/portbw.py','usr/local/sbin/portbw','etc/portbw/config.json',
                 'etc/systemd/system/portbw-watch.timer','etc/systemd/system/portbw-watch.service',
                 'etc/systemd/system/portbw-restore.service']
        for f in tracked:
            target=root/f;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('original '+f)
        (root/'var/tmp').mkdir(parents=True);src=root/'src';src.mkdir();(src/'portbw.py').write_text('new source')
        header=f'''set -Eeuo pipefail
SRC='{src}'
IFACE=eth0
NFT_ONLY=0
log() {{ :; }}
die() {{ echo "$*" >&2; exit 1; }}
systemctl() {{
  case "$1" in
    is-active) [[ "$2" == portbw-watch.timer ]] ;;
    is-enabled) echo enabled ;;
    *) return 0 ;;
  esac
}}
'''
        script=root/'test.sh';script.write_text(header+body+'\ninstall_files\n')
        result=subprocess.run(['bash',str(script)],text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('安装后审计未通过',result.stderr)
        for f in tracked:self.assertEqual((root/f).read_text(),'original '+f)
        self.assertFalse(list((root/'var/tmp').iterdir()))

    def test_bad_hash_stops_installer_before_python(self):
        source=(HERE/'portbw-install.sh').read_text()
        block=source[source.index("EXPECTED_PORTBW_SHA256="):source.index("python3 -B - \"$SRC/portbw.py\"")]
        root=Path(self.temp.name);(root/'portbw.py').write_text('tampered')
        script=root/'hash-test.sh'
        script.write_text('set -Eeuo pipefail\nSRC='+repr(str(root))+"\ndie() { echo \"$*\" >&2; exit 1; }\n"+block)
        out=subprocess.run(['bash',str(script)],text=True,capture_output=True)
        self.assertNotEqual(out.returncode,0);self.assertIn('SHA256 不符',out.stderr)

    def test_installer_hash_repo_and_syntax(self):
        source=(HERE/'portbw-install.sh').read_text()
        expected=re.search(r"EXPECTED_PORTBW_SHA256='([0-9a-f]{64})'",source)[1]
        self.assertEqual(expected,hashlib.sha256((HERE/'portbw.py').read_bytes()).hexdigest())
        self.assertNotIn('zuizhongheji',source)
        self.assertIn('liucong552-art/linshi/refs/heads/main',source)
        self.assertIn('trap cleanup_source EXIT',source)
        self.assertIn('本地缺少 portbw.py',source)
        self.assertIn('raw.githubusercontent.com/',source)
        self.assertNotIn('raw.githubusercontent.com/liucong552-art/zuizhongheji/',source)
        self.assertIn('"$ACTUAL_PORTBW_SHA256" == "$EXPECTED_PORTBW_SHA256"',source)
        subprocess.run(['bash','-n',str(HERE/'portbw-install.sh')],check=True)



# Integration regressions for the merged /run sampling cache.  These are
# intentionally after the upstream unittest.main guard: run with unittest
# discovery (python3 -m unittest -v test_portbw) to include them.
class VolatileCacheTests(Base):
    setup_kernel=WatchIntegrationTests.setup_kernel
    def test_snapshot_failure_invalidates_volatile_streak(self):
        self.setup_kernel({40001:record()})
        def hot(port,d,r,ss,ni):return sample(self.now,(self.now-100)*(1250000 if d=='up' else 0))
        with patch.object(p,'traffic_snapshot',return_value={}),patch.object(p,'traffic_sample',side_effect=hot):
            self.now=100;p.reconcile(self.config,True)
            self.now=130;p.reconcile(self.config,True)
            self.assertTrue(p.volatile_path(40001).exists())
        self.now=150
        with patch.object(p,'ensure_base',side_effect=p.Error('nft unavailable')):
            with self.assertRaises(p.Error):p.reconcile(self.config,True)
        self.assertFalse(p.volatile_path(40001).exists(), 'bad nft snapshot must break cached evidence')
        with patch.object(p,'traffic_snapshot',return_value={}),patch.object(p,'traffic_sample',side_effect=hot):
            self.now=160;p.reconcile(self.config,True)
        self.assertEqual(p.records()[40001]['up'],1250000,'must not add intervals across a failed snapshot')

    def test_corrupt_unrelated_port_does_not_stop_healthy_sampling(self):
        self.setup_kernel({40001:record()})
        damaged=p.port_file(40002);damaged.parent.mkdir(parents=True,exist_ok=True)
        damaged.write_text('\"corrupted\"')
        with patch.object(p,'traffic_snapshot',return_value={}),\
             patch.object(p,'traffic_sample',side_effect=lambda port,d,r,ss,ni:sample(self.now,0)):
            for t in (100,130):
                self.now=t
                with self.assertRaisesRegex(p.Error,'40002'):
                    p.reconcile(self.config,True)
        self.assertTrue(p.volatile_path(40001).exists())
        self.assertEqual(p.records_for_watch()[0][40001]['up'],1250000)
        self.assertTrue(damaged.exists(), 'must not remove corrupted customer configuration')

    def test_healthy_ticks_write_only_tmpfs(self):
        rows={40001:record()};self.setup_kernel(rows)
        def fake_sample(port,d,r,snapshot,nft):
            return sample(self.now,(self.now-100)*(125000 if d=='up' else 0))
        with patch.object(p,'traffic_snapshot',return_value={}),\
             patch.object(p,'traffic_sample',side_effect=fake_sample),\
             patch.object(p,'write_json',wraps=p.write_json) as writer:
            for t in (100,130,160,190,220,250):self.now=t;p.reconcile(self.config,True)
            disk=[x.args[0] for x in writer.call_args_list if x.args[0]==p.port_file(40001)]
            ram=[x.args[0] for x in writer.call_args_list if x.args[0]==p.volatile_path(40001)]
            self.assertEqual(disk,[],'steady monitoring must not fsync the persistent file')
            self.assertEqual(len(ram),6)

    def test_cache_loss_resets_streak_without_false_trigger(self):
        rows={40001:record()};self.setup_kernel(rows)
        def fake_sample(port,d,r,snapshot,nft):
            return sample(self.now,(self.now-100)*(1250000 if d=='up' else 0))
        with patch.object(p,'traffic_snapshot',return_value={}),patch.object(p,'traffic_sample',side_effect=fake_sample):
            for t in (100,130):self.now=t;p.reconcile(self.config,True)
            self.assertEqual(p.records()[40001]['up'],1250000)
            self.assertTrue(p.volatile_path(40001).exists())
            p.volatile_path(40001).unlink()
            self.now=160;p.reconcile(self.config,True)
            self.assertEqual(p.records()[40001]['up'],1250000,'lost cache may not finish previous 30s streak')
            self.now=190;p.reconcile(self.config,True)
            self.assertEqual(p.records()[40001]['up'],1250000)
            self.now=220;p.reconcile(self.config,True)
            self.assertEqual(p.records()[40001]['up'],625000,'new valid full streak should eventually trigger')

    def test_cache_loss_does_not_end_persisted_hold(self):
        r=record();r['up']=625000;r['runtime']['dirs']['up'].update(phase='hold',until=250)
        self.setup_kernel({40001:r})
        self.now=170
        with patch.object(p,'traffic_snapshot',side_effect=p.Error('stats missing')):
            with self.assertRaises(p.Error):p.reconcile(self.config,True)
        self.assertEqual(p.records()[40001]['up'],625000)
        self.now=251
        with patch.object(p,'traffic_snapshot',side_effect=p.Error('stats missing')):
            with self.assertRaises(p.Error):p.reconcile(self.config,True)
        self.assertEqual(p.records()[40001]['up'],1250000,'durable hold deadline must still restore on expiry')

    def test_cache_never_overrides_new_manual_rate_intent(self):
        r=record();self.setup_kernel({40001:r})
        self.now=100
        with patch.object(p,'traffic_snapshot',return_value={}),\
             patch.object(p,'traffic_sample',side_effect=lambda port,d,rec,ss,ni:sample(self.now,0)):
            p.reconcile(self.config,True)
        self.assertTrue(p.volatile_path(40001).exists())
        # Simulate a newer durable manual commit (the crash happens before
        # any volatile cache cleanup). The saved record must win.
        modified=p.records()[40001];modified['up']=1250000*2
        modified['auto']['base']['up']=1250000*2
        modified['updated']=modified.get('updated',0)+1
        p.write_json(p.port_file(40001),modified)
        loaded=p.records()[40001]
        self.assertFalse(p.volatile_load(40001,loaded))
        self.assertEqual(loaded['up'],2500000)


class DamagedSiblingRegressionTests(Base):
    """Regression: corrupt config must not block known-owned tc transitions.

    No kernel commands are executed; state changes are simulated while the
    actual record/pref safety checks and watchdog state machine execute.
    """

    def test_corrupt_sibling_allows_existing_port_to_actually_throttle(self):
        r=record();p.write_json(p.port_file(40001),r)
        bad=p.port_file(40002);bad.write_text('"corrupted"')
        limits={'nft':dict(up=r['up'],down=r['down']),
                'tc':dict(up=r['up'],down=r['down'])}
        slots=tc_fixture(40001,r)['rows']
        def healthy(kind,rec):return all(limits[kind][d]==rec[d] for d in p.CHAIN)
        def sample_load(port,d,rec,snapshot,items):
            return sample(self.now,(self.now-100)*(1250000 if d=='up' else 0))
        def mark_applied(kind,port,rec,*args):
            limits[kind].update({d:rec[d] for d in p.CHAIN})
        with patch.object(p,'ensure_base',return_value=[]),\
             patch.object(p,'tc_snapshot',return_value={}),\
             patch.object(p,'traffic_snapshot',return_value={}),\
             patch.object(p,'traffic_sample',side_effect=sample_load),\
             patch.object(p,'tc_filters',side_effect=lambda iface,td:slots[td]),\
             patch.object(p,'nft_ok',side_effect=lambda port,rec,*arg:healthy('nft',rec)),\
             patch.object(p,'tc_ok',side_effect=lambda port,rec,*arg:healthy('tc',rec)),\
             patch.object(p,'apply_nft',side_effect=lambda port,rec:mark_applied('nft',port,rec)) as nft_apply,\
             patch.object(p,'apply_tc',side_effect=lambda port,rec,iface:mark_applied('tc',port,rec)) as tc_apply:
            for t in (100,130,160):
                self.now=t
                with self.assertRaisesRegex(p.Error,'40002'):
                    p.reconcile(self.config,monitor=True)
        healthy_rec=p.read_json(p.port_file(40001))
        self.assertEqual(healthy_rec['up'],625000)
        self.assertFalse(healthy_rec['pending'])
        self.assertEqual(healthy_rec['runtime']['dirs']['up']['phase'],'hold')
        self.assertEqual(limits['tc']['up'],625000)
        self.assertEqual(limits['nft']['up'],625000)
        self.assertTrue(bad.exists())
        tc_apply.assert_called_once()
        nft_apply.assert_called_once()

    def test_corrupt_sibling_blocks_new_tc_pref_allocation(self):
        r=record();r.pop('tc_prefs');r['pending']=True
        p.write_json(p.port_file(40001),r)
        p.port_file(40002).write_text('"corrupted"')
        with patch.object(p,'ensure_base',return_value=[]),\
             patch.object(p,'tc_snapshot',return_value={}),\
             patch.object(p,'nft_ok',return_value=True),\
             patch.object(p,'tc_ok',return_value=False),\
             patch.object(p,'tc_filters',return_value=[]),\
             patch.object(p,'apply_tc') as tc_apply:
            with self.assertRaisesRegex(p.Error,'拒绝分配新的 tc pref'):
                p.reconcile(self.config,False)
        persisted=p.read_json(p.port_file(40001))
        self.assertTrue(persisted['pending'])
        self.assertNotIn('tc_prefs',persisted)
        tc_apply.assert_not_called()

    def test_corrupt_sibling_rejects_new_slot_before_nft_mutation(self):
        r=record();r.pop('tc_prefs');r['pending']=True
        with patch.object(p,'nft_ok',return_value=False),\
             patch.object(p,'apply_nft') as nft_apply,\
             patch.object(p,'ensure_tc_prefs') as tc_alloc:
            with self.assertRaisesRegex(p.Error,'拒绝分配新的 tc pref'):
                p.apply(40001,r,self.config,watch_rows={40001:r},damaged_siblings=True)
        nft_apply.assert_not_called()
        tc_alloc.assert_not_called()

    def test_corrupt_sibling_does_not_override_foreign_tc_pref(self):
        r=record();r['pending']=True
        p.write_json(p.port_file(40001),r)
        p.port_file(40002).write_text('"corrupted"')
        foreign={'pref':r['tc_prefs']['4_tcp'],'protocol':'ip','kind':'flower','chain':0,
                 'options':{'handle':'0x1', 'keys':{'ip_proto':'udp','dst_port':40002}}}
        def filters(iface,td):return [foreign] if td=='ingress' else []
        with patch.object(p,'ensure_base',return_value=[]),\
             patch.object(p,'tc_snapshot',return_value={}),\
             patch.object(p,'nft_ok',return_value=True),\
             patch.object(p,'tc_ok',return_value=False),\
             patch.object(p,'tc_filters',side_effect=filters),\
             patch.object(p,'apply_tc') as tc_apply:
            with self.assertRaisesRegex(p.Error,'非本模块 tc 规则'):
                p.reconcile(self.config,False)
        self.assertTrue(p.read_json(p.port_file(40001))['pending'])
        tc_apply.assert_not_called()

    def test_interactive_pref_mutation_remains_strict_with_corrupt_sibling(self):
        r=record();p.write_json(p.port_file(40001),r)
        p.port_file(40002).write_text('"corrupted"')
        with patch.object(p,'tc_filters',return_value=[]):
            with self.assertRaises(p.Error):
                p.ensure_tc_prefs(40001,r,'eth0')

    def test_corrupt_sibling_does_not_block_hold_expiry_restoration(self):
        r=record();r['up']=625000;r['runtime']['dirs']['up'].update(phase='hold',until=250)
        p.write_json(p.port_file(40001),r)
        p.port_file(40002).write_text('"corrupted"')
        limit=dict(up=r['up'],down=r['down'])
        with patch.object(p,'ensure_base',return_value=[]),\
             patch.object(p,'tc_snapshot',return_value={}),\
             patch.object(p,'traffic_snapshot',return_value={}),\
             patch.object(p,'traffic_sample',return_value=sample(251,0)),\
             patch.object(p,'tc_filters',return_value=[]),\
             patch.object(p,'nft_ok',side_effect=lambda port,rec,*args:rec['up']==limit['up']),\
             patch.object(p,'tc_ok',side_effect=lambda port,rec,*args:rec['up']==limit['up']),\
             patch.object(p,'apply_nft',side_effect=lambda port,rec:limit.update(up=rec['up'])),\
             patch.object(p,'apply_tc',side_effect=lambda port,rec,iface:limit.update(up=rec['up'])):
            self.now=251
            with self.assertRaisesRegex(p.Error,'40002'):
                p.reconcile(self.config,True)
        saved=p.read_json(p.port_file(40001))
        self.assertEqual(saved['up'],1250000)
        self.assertEqual(saved['runtime']['dirs']['up']['phase'],'monitor')
        self.assertFalse(saved['pending'])

if __name__=='__main__':unittest.main(verbosity=2)
