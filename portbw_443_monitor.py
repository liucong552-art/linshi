#!/usr/bin/env python3
"""Read-only, 1-second portbw tc police probe. Tested with portbw v5.1 text format.
Does not change kernel, tc, nft, Xray, or systemd state.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time

ACTION = re.compile(r'^\s*action order \d+:\s+police\s+0x([0-9a-fA-F]+)\b', re.M)
RATE = re.compile(r'\brate\s+([0-9]+(?:\.[0-9]+)?)([kKmMgGtT]?)bit\b')
BYTES = re.compile(r'\bSent\s+(\d+)\s+bytes\s+\d+\s+pkt\b')
DROPPED = re.compile(r'\bdropped\s+(\d+)\b')
OVERLIMITS = re.compile(r'\boverlimits\s+(\d+)\b')


def read_kernel_action(port):
    idx = 0x6D000000 + 2 * port + 1  # egress/download, source port N
    output = subprocess.check_output(
        ['tc', '-s', 'actions', 'ls', 'action', 'police'],
        text=True, stderr=subprocess.PIPE, timeout=5)
    matches = list(ACTION.finditer(output))
    found = [(i, m) for i, m in enumerate(matches) if int(m[1], 16) == idx]
    if len(found) != 1:
        raise RuntimeError(f'expected exactly one police 0x{idx:x}, got {len(found)}')
    i, match = found[0]
    block = output[match.start():matches[i+1].start() if i+1 < len(matches) else len(output)]
    m_rate = RATE.search(block)
    m_bytes = BYTES.search(block)
    m_dropped = DROPPED.search(block)
    m_over = OVERLIMITS.search(block)
    if not all((m_rate, m_bytes, m_dropped, m_over)):
        raise RuntimeError('incomplete police statistics: ' + block[:350].replace('\n', ' | '))
    multiplier = {'': 1, 'k': 1_000, 'm': 1_000_000,
                  'g': 1_000_000_000, 't': 1_000_000_000_000}[m_rate[2].lower()]
    return (float(m_rate[1]) * multiplier / 1e6,
            int(m_bytes[1]), int(m_dropped[1]), int(m_over[1]))


def read_port_state(port):
    p = Path(f'/var/lib/portbw/ports/{port}.json')
    saved = json.loads(p.read_text())
    target = saved['down'] * 8 / 1e6
    rt = saved.get('runtime', {})
    temp = Path(f'/run/portbw/auto/{port}.json')
    try:
        cache = json.loads(temp.read_text())
        cp = cache['checkpoint']
        cd = cp['dirs']['down']
        sd = rt['dirs']['down']
        if (cache.get('boot_id') == rt.get('boot_id') and
            cp.get('updated') == saved.get('updated') and
            cp.get('auto') == saved.get('auto') and
            cd.get('target') == saved['down'] and
            cd.get('phase') == sd.get('phase') and
            cd.get('until') == sd.get('until')):
            rt = cache['runtime']
    except (OSError, KeyError, ValueError, TypeError):
        pass
    s = rt.get('dirs', {}).get('down', {})
    return target, s.get('phase', '?'), s.get('progress', '')


def main():
    parser = argparse.ArgumentParser(description='Portbw download 1-second server-only observation; no kernel writes.')
    parser.add_argument('--port', type=int, default=443)
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--interval', type=float, default=1.0)
    parser.add_argument('--csv', default='/root/portbw-443-monitor.csv')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.seconds <= 0 or args.interval < 0.2:
        parser.error('invalid port, seconds, or interval')
    print(f'Read-only probe: port={args.port}, interval={args.interval}s, log={args.csv}', flush=True)
    prev = None
    prev_rate = None
    began = time.monotonic()
    next_tick = began
    columns = ['utc_time', 'kernel_rate_mbps', 'action_seen_mbps',
               'dropped_packets_delta', 'overlimits_delta', 'config_target_mbps',
               'down_phase', 'high_progress_s', 'note']
    with open(args.csv, 'w', newline='') as log:
        writer = csv.writer(log)
        writer.writerow(columns)
        try:
            while time.monotonic() - began <= args.seconds:
                stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
                now = time.monotonic()
                seen = drops = over = None
                note = ''
                try:
                    rate, count, drop, overcount = read_kernel_action(args.port)
                    target, phase, progress = read_port_state(args.port)
                    if prev is not None:
                        elapsed = now - prev[0]
                        if count >= prev[1] and drop >= prev[2] and overcount >= prev[3] and elapsed > 0:
                            seen = (count - prev[1]) * 8 / elapsed / 1e6
                            drops = drop - prev[2]
                            over = overcount - prev[3]
                        else:
                            note = 'counters_reset_or_action_replaced'
                    if prev_rate is not None and prev_rate != rate:
                        note = (note + ' ' if note else '') + f'RATE_CHANGED:{prev_rate:g}->{rate:g}'
                    prev = (now, count, drop, overcount)
                    prev_rate = rate
                except (OSError, ValueError, subprocess.SubprocessError, RuntimeError, KeyError) as e:
                    rate = target = None
                    phase = '?'
                    progress = ''
                    note = 'ERROR:' + str(e)[:150]
                    prev = None
                def fnum(value):
                    return '' if value is None else f'{value:.2f}'
                writer.writerow([stamp, fnum(rate), fnum(seen), drops, over,
                                 fnum(target), phase, progress, note])
                log.flush()
                print(f'{stamp}  tc={fnum(rate) or "?":>6} Mbps  '
                      f'action_seen={fnum(seen) or "?":>7} Mbps  '
                      f'drops={str(drops) if drops is not None else "?":>5}  '
                      f'target={fnum(target) or "?":>6}  phase={phase:<10} {note}', flush=True)
                next_tick += args.interval
                time.sleep(max(0, next_tick - time.monotonic()))
        except KeyboardInterrupt:
            print('Interrupted: partial CSV is saved.', flush=True)
    print(f'Saved {args.csv}', flush=True)


if __name__ == '__main__':
    main()
