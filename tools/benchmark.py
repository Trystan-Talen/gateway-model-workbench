"""Finite loopback engine benchmark. Never accepts a remote target or credentials."""
import asyncio
import argparse
import json
import platform
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from smart_route_load import do_request


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.dumps({'model':'loopback-benchmark','choices':[{'message':{'content':'pong'},'finish_reason':'stop'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def benchmark(url, count, concurrency):
    gate=asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(trust_env=False) as client:
        async def once(index):
            async with gate:
                return await do_request(client, {'id':index}, {'model':'loopback-benchmark','messages':[{'role':'user','content':'ping'}],'stream':False}, {}, {'connect':5,'read':10,'write':5,'pool':5}, chat_url=url, include_usage=False)
        start=time.perf_counter()
        records=await asyncio.gather(*(once(i) for i in range(count)))
        elapsed=time.perf_counter()-start
    durations=sorted(r['total_ms'] for r in records if r.get('total_ms') is not None)
    return {'requests':count,'concurrency':concurrency,'elapsed_seconds':round(elapsed,3),
            'completed_rpm':round(count*60/elapsed,1),'successes':sum(bool(r.get('completed')) for r in records),
            'p50_ms':statistics.median(durations) if durations else None,
            'p95_ms':durations[int((len(durations)-1)*.95)] if durations else None}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests',type=int,default=200)
    parser.add_argument('--concurrency',type=int,default=16)
    args=parser.parse_args()
    if not 1<=args.requests<=10000 or not 1<=args.concurrency<=128:
        parser.error('请求数限 1–10000，并发限 1–128')
    server=ThreadingHTTPServer(('127.0.0.1',0),MockUpstream)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        result=asyncio.run(benchmark(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',args.requests,args.concurrency))
        result.update(python=platform.python_version(),platform=platform.platform(),scope='本机模拟接口与请求引擎的联合基线；不代表上游网关容量，也不含流式长文本与持久化成本。')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':main()
