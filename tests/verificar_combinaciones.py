import sys,pathlib,json,urllib.request,urllib.parse,urllib.error,time
sys.dont_write_bytecode=True
p=pathlib.Path(__file__).resolve().parent.parent;sys.path.insert(0,str(p));import server as a
rows=a._exec('SELECT DISTINCT period_start,zone_id,circuit_id,tariff_id,client_type FROM energrid.mat_consumo_estimado_mes WHERE zone_id>0 AND circuit_id>0 AND tariff_id>0')
zones=a._exec('SELECT zone_id,region FROM energrid.dim_zona WHERE zone_id>0');regions={z['zone_id']:z['region'] for z in zones}
cases=[]
for z in zones:cases.append(dict(zones=str(z['zone_id'])))
for key in sorted({(r['client_type'],r['tariff_id']) for r in rows}):cases.append(dict(client_type=key[0],tariff=key[1]))
for r in rows[::max(1,len(rows)//30)][:30]:cases.append(dict(zones=str(r['zone_id']),region=regions[r['zone_id']],circuit=r['circuit_id'],tariff=r['tariff_id'],client_type=r['client_type'],month='2025-12'))
results=[]
for params in cases:
 for endpoint in ['/api/filters','/api/dashboard']:
  url='http://127.0.0.1:8000'+endpoint+'?'+urllib.parse.urlencode(params)
  with urllib.request.urlopen(url,timeout=15) as response:
   data=json.load(response);assert response.status==200
   if endpoint.endswith('filters'):assert data['valid']
  results.append(dict(endpoint=endpoint,params=params,status=200))
for tariff,ctype in [(2,'Residencial'),(2,'Industrial'),(1,'Comercial'),(4,'Comercial'),(3,'Residencial')]:
 url='http://127.0.0.1:8000/api/dashboard?'+urllib.parse.urlencode(dict(tariff=tariff,client_type=ctype))
 try:urllib.request.urlopen(url,timeout=15);raise AssertionError('Se acepto combinacion incompatible')
 except urllib.error.HTTPError as e:assert e.code==400;results.append(dict(endpoint='invalido',tariff=tariff,client_type=ctype,status=e.code))
(p/'logs').mkdir(exist_ok=True);(p/'logs/verificacion_combinaciones.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print('PASS',len(results),'peticiones HTTP reales con SQL: filtros validos y cinco cruces incompatibles sin 500')
indexes=a._exec("SELECT t.name tabla,i.name indice FROM sys.tables t JOIN sys.indexes i ON i.object_id=t.object_id WHERE t.schema_id=SCHEMA_ID('energrid') AND i.name LIKE 'ix_rend%' ORDER BY t.name,i.name")
(p/'logs/indices_aplicados.json').write_text(json.dumps(indexes,indent=2),encoding='utf-8');print('Indices de rendimiento comprobados:',len(indexes))
# CSV completo de combinaciones facturadas, sin datos identificables de clientes.
import csv
with open(p/'logs/combinaciones_validas.csv','w',encoding='utf-8',newline='') as f:
 w=csv.DictWriter(f,fieldnames=['period_start','zone_id','circuit_id','tariff_id','client_type']);w.writeheader();w.writerows(sorted(rows,key=lambda r:(str(r['period_start']),r['zone_id'],r['circuit_id'],r['tariff_id'],r['client_type'])))
print('Combinaciones periodo/zona/circuito/tarifa/tipo:',len(rows))
