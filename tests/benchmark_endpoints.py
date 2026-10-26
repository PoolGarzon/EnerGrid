import argparse,json,time,urllib.request,urllib.error,urllib.parse,statistics,math,pathlib
parser=argparse.ArgumentParser();parser.add_argument('--base-url',default='http://127.0.0.1:8000');parser.add_argument('--repetitions',type=int,default=5);parser.add_argument('--output',default='benchmark.json');parser.add_argument('--sql',action='store_true');args=parser.parse_args()
if args.repetitions<5:parser.error('Se requieren al menos 5 repeticiones, ademas de la primera')
def get(path,params=None):
    url=args.base_url+path+('?' + urllib.parse.urlencode(params) if params else '')
    t=time.perf_counter()
    try:
        with urllib.request.urlopen(url,timeout=180) as r:body=r.read();status=r.status;headers=dict(r.headers)
    except urllib.error.HTTPError as e:body=e.read();status=e.code;headers=dict(e.headers)
    return dict(ms=(time.perf_counter()-t)*1000,status=status,bytes=len(body),body=json.loads(body),server_timing=headers.get('Server-Timing',headers.get('server-timing')),sql_queries=headers.get('x-sql-queries',headers.get('X-SQL-Queries')))
c=get('/api/filters')['body'];zs=get('/api/zones')['body'];zone=zs[0]['zone_id'];circuit=next(x['circuit_id'] for x in c['circuits'] if x['zone_id']==zone)
cases=[('catalogo','/api/filters',{}),('sin_filtros','/api/dashboard',{}),('zona','/api/dashboard',dict(zones=str(zone))),('zona_circuito','/api/dashboard',dict(zones=str(zone),circuit=circuit)),('tarifa','/api/dashboard',dict(tariff=2)),('tipo','/api/dashboard',dict(client_type='Comercial')),('combinados','/api/dashboard',dict(zones=str(zone),circuit=circuit,tariff=2,client_type='Comercial',month='2025-12')),('invalida','/api/dashboard',dict(tariff=2,client_type='Residencial'))]
results=[]
for name,path,params in cases:
    started=time.perf_counter();first=get(path,params);ready=first
    while ready['status']==200 and ready['body'].get('top10_loading') and time.perf_counter()-started<120:
        time.sleep(.05);ready=get(path,params)
    ready_ms=(time.perf_counter()-started)*1000
    warm=[get(path,params) for _ in range(args.repetitions)];times=sorted(x['ms'] for x in warm)
    row=dict(case=name,path=path,params=params,first_ms=first['ms'],first_status=first['status'],first_server_timing=first['server_timing'],first_sql_queries=first['sql_queries'],min_ms=min(times),median_ms=statistics.median(times),mean_ms=statistics.mean(times),p95_ms=times[math.ceil(.95*len(times))-1],max_ms=max(times),bytes=warm[-1]['bytes'],statuses=[x['status'] for x in warm],server_timing=warm[-1]['server_timing'],time_to_ready_ms=ready_ms,ready_pending=ready['body'].get('top10_loading'),sql_queries=warm[-1]['sql_queries'],first_pending=first['body'].get('top10_loading'),warm_pending=[x['body'].get('top10_loading') for x in warm])
    if first['status']>=400:row['error']=first['body']
    results.append(row);print(name,round(first['ms'],1),round(row['median_ms'],1),first['status'],flush=True)
    pathlib.Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    pathlib.Path(args.output).write_text(json.dumps(dict(base_url=args.base_url,repetitions=args.repetitions,results=results),ensure_ascii=False,indent=2),encoding='utf-8')

if args.sql:
    # El usuario/clave se leen solo del entorno por config.py. Sin valores en el reporte.
    import sys,re
    root=pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0,str(root))
    import server as app
    import pyodbc
    sql_results=[]
    try:
        con=pyodbc.connect(app.conn_str(),timeout=10)
        exists=con.execute("SELECT OBJECT_ID(N'energrid.mat_criticidad_transformador_full')").fetchval()
        for label,table in [('vista_original','vw_criticidad_transformador')]+([('materializado','mat_criticidad_transformador_full')] if exists else []):
            query=app._top10_selection_sql().replace('vw_criticidad_transformador',table)
            query,params=app.bind(query,dict(top_all_zones=0,top_zones=[zone],top_circuit=None))
            samples=[];messages=[]
            for i in range(args.repetitions+1):
                cur=con.cursor();t=time.perf_counter()
                cur.execute('SET STATISTICS IO ON; SET STATISTICS TIME ON; '+query,params)
                while True:
                    if cur.description:cur.fetchall()
                    messages += [str(m[1]) for m in cur.messages]
                    if not cur.nextset():break
                samples.append((time.perf_counter()-t)*1000);cur.close()
            warm=sorted(samples[1:])
            sql_results.append(dict(case=label,first_ms=samples[0],min_ms=min(warm),median_ms=statistics.median(warm),mean_ms=statistics.mean(warm),p95_ms=warm[math.ceil(.95*len(warm))-1],max_ms=max(warm),messages=messages))
        con.close()
    except Exception as exc:
        sql_results.append(dict(error=str(exc)[:250]))
    output=pathlib.Path(args.output)
    saved=json.loads(output.read_text(encoding='utf-8'));saved['sql']=sql_results
    output.write_text(json.dumps(saved,ensure_ascii=False,indent=2),encoding='utf-8')
