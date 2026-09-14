"""Reproducible Grafana dashboards, no credentials or customer observations."""
import json
from pathlib import Path
root=Path(__file__).parent
panels=[]
def panel(title,expr,unit='short',legend='{{host}}',kind='timeseries'):
 i=len(panels);panels.append({'id':i+1,'title':title,'type':kind,'datasource':{'type':'prometheus','uid':'ops-prometheus'},'gridPos':{'x':(i%2)*12,'y':(i//2)*8,'w':12,'h':8},'targets':[{'refId':'A','expr':expr,'legendFormat':legend}],'fieldConfig':{'defaults':{'unit':unit,'custom':{'drawStyle':'line','fillOpacity':12,'spanNulls':False}},'overrides':[]},'options':{'legend':{'displayMode':'table','placement':'bottom','calcs':['lastNotNull','max','mean']},'tooltip':{'mode':'multi','sort':'desc'}}})
h='{host=~"$machine"}'
panel('Disponibilité des machines','ops_infra_up'+h,'bool',kind='stat')
panels[-1]['fieldConfig']['defaults']['mappings']=[{'type':'value','options':{'0':{'text':'Hors ligne','color':'red'},'1':{'text':'En ligne','color':'green'}}}]
panel('Cœurs logiques disponibles','ops_infra_cpu_cores'+h,kind='stat')
panel('CPU · utilisation','ops_infra_cpu_usage_percent'+h,'percent')
panel('RAM · utilisée / capacité','100*ops_infra_memory_used_bytes'+h+'/ops_infra_memory_total_bytes'+h,'percent')
panel('CPU · par cœur','ops_infra_cpu_core_usage_percent'+h,'percent','{{host}} · cœur {{core}}')
panel('Charge système','ops_infra_load'+h,'short','{{host}} · {{minutes}} min')
panel('RAM disponible','ops_infra_memory_available_bytes'+h,'bytes')
panel('Swap utilisée','ops_infra_memory_swap_used_bytes'+h,'bytes')
panel('Volumes · espace utilisé','100*ops_infra_filesystem_used_bytes'+h+'/ops_infra_filesystem_total_bytes'+h,'percent','{{host}} · {{mount}}')
panel('Volumes · espace disponible','ops_infra_filesystem_available_bytes'+h,'bytes','{{host}} · {{mount}}')
panel('Volumes · inodes utilisés','100*(1-ops_infra_filesystem_inodes_free'+h+'/ops_infra_filesystem_inodes_total'+h+')','percent','{{host}} · {{mount}}')
panel('Stockage · croissance horaire','deriv(ops_infra_filesystem_used_bytes'+h+'[1h])*3600','bytes','{{host}} · {{mount}}')
panel('Réseau · réception','ops_infra_network_rx_bytes_per_second'+h,'Bps','{{host}} · {{interface}}')
panel('Réseau · émission','ops_infra_network_tx_bytes_per_second'+h,'Bps','{{host}} · {{interface}}')
panel('Réseau · erreurs reçues par seconde','rate(ops_infra_network_rx_errors'+h+'[5m])','ops','{{host}} · {{interface}}')
panel('Réseau · paquets perdus par seconde','rate(ops_infra_network_rx_dropped'+h+'[5m])','pps','{{host}} · {{interface}}')
panel('Disques · lecture','ops_infra_disk_read_bytes_per_second'+h,'Bps','{{host}} · {{device}}')
panel('Disques · écriture','ops_infra_disk_write_bytes_per_second'+h,'Bps','{{host}} · {{device}}')
panel('Disques · occupation E/S','ops_infra_disk_busy_ms_per_second'+h+'/10','percent','{{host}} · {{device}}')
panel('Nombre de processus','ops_infra_process_count'+h)
panel('Températures','ops_infra_temperature_celsius'+h,'celsius','{{host}} · {{sensor}}')
panel('Temps depuis démarrage','ops_infra_uptime_seconds'+h,'s')
panel('GPU · utilisation','ops_infra_gpu_usage_pct'+h,'percent','{{host}} · {{gpu}}')
panel('GPU · consommation mesurée','ops_infra_gpu_power_w'+h,'watt','{{host}} · {{gpu}}')
panel('Alertes actives','ALERTS{alertstate="firing"}','short','{{alertname}}',kind='table')
panel('Collecte · ancienneté','time()-ops_infra_last_received_timestamp_seconds'+h,'s')
x={'uid':'ops-infrastructure','title':'Ops · Infrastructure complète','schemaVersion':39,'version':1,'editable':False,'timezone':'browser','refresh':'15s','time':{'from':'now-1h','to':'now'},'tags':['ops','infrastructure'],'templating':{'list':[{'name':'machine','label':'Machine','type':'query','datasource':{'uid':'ops-prometheus','type':'prometheus'},'query':'label_values(ops_infra_up, host)','refresh':1,'multi':True,'includeAll':True,'allValue':'.*','current':{'text':'All','value':'$__all'}}]},'annotations':{'list':[{'name':'Incidents','enable':False,'datasource':{'uid':'ops-prometheus','type':'prometheus'},'expr':'ALERTS{alertstate="firing"}','titleFormat':'{{alertname}}','textFormat':'{{host}} {{severity}}','iconColor':'#ff7373'}]},'panels':panels}
(root/'infrastructure.json').write_text(json.dumps(x,indent=2)+'\n')
