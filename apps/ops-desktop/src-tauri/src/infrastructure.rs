use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

#[tauri::command]
pub fn infrastructure_snapshot() -> Result<serde_json::Value, String> {
    let file=std::fs::OpenOptions::new().read(true).custom_flags(0o400000)
        .open("/run/ops-telemetry/dashboard.json")
        .map_err(|_|"Les premières mesures ne sont pas encore disponibles.".to_string())?;
    let meta=file.metadata().map_err(|_|"État des mesures indisponible")?;
    if !meta.is_file() || meta.uid()!=0 || meta.mode() & 0o022 != 0 || meta.len()>8*1024*1024 {
        return Err("Le fichier de mesures n’est pas conforme.".into());
    }
    let mut bytes=Vec::new();file.take(8*1024*1024+1).read_to_end(&mut bytes).map_err(|_|"Lecture des mesures impossible")?;
    if bytes.len()>8*1024*1024 {return Err("Mesures trop volumineuses".into());}
    let value:serde_json::Value=serde_json::from_slice(&bytes).map_err(|_|"Mesures incomplètes")?;
    if value["schema_version"]!=1 || !value["hosts"].is_array(){return Err("Format des mesures inconnu".into());}
    Ok(value)
}

#[tauri::command]
pub async fn infrastructure_history(host: String, timestamp: u64) -> Result<serde_json::Value,String> {
    if !matches!(host.as_str(),"dell-control"|"nas"|"prod"|"edge-vps"|"gamebox") || timestamp==0 || timestamp>4_102_444_800 {return Err("Date ou machine invalide".into());}
    tauri::async_runtime::spawn_blocking(move || {
        let output=std::process::Command::new("/usr/bin/python3").args(["-I","/usr/local/libexec/ops-infrastructure-query.py",&host,&timestamp.to_string()])
          .stdin(std::process::Stdio::null()).stderr(std::process::Stdio::null()).output().map_err(|_|"Lecture d’archive impossible".to_string())?;
        if output.stdout.len()>1024*1024 {return Err("Archive trop volumineuse".into());}
        let value:serde_json::Value=serde_json::from_slice(&output.stdout).map_err(|_|"Archive indisponible".to_string())?;
        if !output.status.success() || value["ok"]!=true {return Err(value["error"].as_str().unwrap_or("Archive indisponible").to_string());}
        Ok(value)
    }).await.map_err(|_|"Lecture d’archive interrompue".to_string())?
}

#[tauri::command]
pub async fn infrastructure_logs(request:serde_json::Value)->Result<serde_json::Value,String>{
    use std::os::unix::fs::FileTypeExt;
    let host=request["host"].as_str().unwrap_or("");
    if !matches!(host,"dell-control"|"nas"|"prod"|"edge-vps"|"gamebox") || !matches!(request["action"].as_str(),Some("read"|"sources"|"archive"|"health")){return Err("Demande de journaux invalide".into());}
    let payload=serde_json::to_vec(&request).map_err(|_|"Demande invalide".to_string())?;
    if payload.len()>4095{return Err("Demande trop volumineuse".into());}
    tauri::async_runtime::spawn_blocking(move||{
      use std::io::Write;
      let path="/run/ops-logs/query.sock";
      let meta=std::fs::symlink_metadata(path).map_err(|_|"Service des journaux indisponible".to_string())?;
      if !meta.file_type().is_socket()||meta.uid()!=0||meta.mode()&0o007!=0{return Err("Service des journaux non conforme".into());}
      let mut socket=std::os::unix::net::UnixStream::connect(path).map_err(|_|"Connexion aux journaux impossible".to_string())?;
      socket.set_read_timeout(Some(std::time::Duration::from_secs(45))).map_err(|_|"Délai invalide".to_string())?;
      socket.set_write_timeout(Some(std::time::Duration::from_secs(5))).map_err(|_|"Délai invalide".to_string())?;
      socket.write_all(&payload).and_then(|_|socket.write_all(b"\n")).map_err(|_|"Envoi impossible".to_string())?;
      let mut bytes=Vec::new();socket.take(1024*1024+1).read_to_end(&mut bytes).map_err(|_|"Lecture interrompue".to_string())?;
      if bytes.len()>1024*1024{return Err("Journaux trop volumineux".into());}
      let result:serde_json::Value=serde_json::from_slice(&bytes).map_err(|_|"Réponse des journaux invalide".to_string())?;
      if let Some(e)=result["error"].as_str(){return Err(e.to_string());}Ok(result)
    }).await.map_err(|_|"Lecture interrompue".to_string())?
}

#[tauri::command]
pub fn infrastructure_export_logs(payload:serde_json::Value)->Result<String,String>{
    use std::io::Write;
    use std::os::unix::fs::DirBuilderExt;
    let host=payload["host"].as_str().unwrap_or("");
    if !matches!(host,"dell-control"|"nas"|"prod"|"edge-vps"|"gamebox"){return Err("Machine invalide".into());}
    let rows=payload["records"].as_array().ok_or("Journaux invalides")?;
    if rows.len()>300{return Err("Export trop volumineux".into());}
    let mut output=Vec::new();
    for row in rows{
      let message=row["message"].as_str().ok_or("Message invalide")?;
      if message.len()>8192{return Err("Message trop volumineux".into());}
      let value=serde_json::json!({"host":host,"t":row["t"],"source":row["source"],"service":row["service"],"level":row["level"],"message":message});
      serde_json::to_writer(&mut output,&value).map_err(|_|"Export invalide")?;output.push(b'\n');
    }
    if output.len()>1024*1024{return Err("Export trop volumineux".into());}
    let folder=std::path::Path::new("/home/ops-user/Documents/Ops-Exports");
    std::fs::DirBuilder::new().recursive(true).mode(0o700).create(folder).map_err(|_|"Dossier d’export indisponible")?;
    if !std::fs::symlink_metadata(folder).map_err(|_|"Dossier indisponible")?.is_dir(){return Err("Dossier d’export non conforme".into());}
    let stamp=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|_|"Horloge invalide")?.as_nanos();
    let path=folder.join(format!("journaux-{host}-{stamp}.jsonl"));
    let mut file=std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path).map_err(|_|"Création de l’export impossible")?;
    file.write_all(&output).and_then(|_|file.sync_all()).map_err(|_|"Écriture de l’export impossible")?;
    Ok(path.to_string_lossy().into())
}

fn metric_query(metric: u64, host: &str, start: u64, end: u64) -> Result<(String,u64),String> {
    if !matches!(host,"all"|"dell-control"|"nas"|"prod"|"edge-vps"|"gamebox") || start==0 || end<=start || end-start>366*86400 || end>4_102_444_800 {
        return Err("Machine ou période invalide (un an maximum).".into());
    }
    let catalog:serde_json::Value=serde_json::from_str(include_str!("infrastructure-metrics.json")).map_err(|_|"Catalogue indisponible")?;
    let panel=catalog.as_array().unwrap().iter().find(|p|p["id"].as_u64()==Some(metric)).ok_or("Mesure inconnue")?;
    let expression=panel["expr"].as_str().unwrap().replace("$machine",if host=="all"{".*"}else{host});
    // Alerts also follow the selected machine; unlabelled global alerts belong to the overview.
    let expression=if metric==25 && host!="all" {format!("ALERTS{{alertstate=\"firing\",host=\"{host}\"}}") } else {expression};
    Ok((expression,((end-start)/240).max(15)))
}

#[tauri::command]
pub async fn infrastructure_series(metric:u64,host:String,start:u64,end:u64)->Result<serde_json::Value,String>{
    let (expression,step)=metric_query(metric,&host,start,end)?;
    tauri::async_runtime::spawn_blocking(move||{
        let mut url:tauri::Url="http://127.0.0.1:9090/api/v1/query_range".parse().unwrap();
        url.query_pairs_mut().append_pair("query",&expression).append_pair("start",&start.to_string()).append_pair("end",&end.to_string()).append_pair("step",&step.to_string()).append_pair("timeout","8s");
        let output=std::process::Command::new("/usr/bin/curl")
            .args(["--disable","--noproxy","*","--proto","=http","--max-time","12","--max-filesize","8388608","--silent","--fail",url.as_str()])
            .stdin(std::process::Stdio::null()).stderr(std::process::Stdio::null()).output().map_err(|_|"Lecture des séries impossible")?;
        if !output.status.success() || output.stdout.len()>8*1024*1024 {return Err("Historique détaillé indisponible. Réessayez ou réduisez la période.".into());}
        let value:serde_json::Value=serde_json::from_slice(&output.stdout).map_err(|_|"Réponse des mesures invalide")?;
        if value["status"]!="success" {return Err("Lecture des mesures refusée".into());}
        let rows=value["data"]["result"].as_array().ok_or("Séries absentes")?;
        // Bound the chart while reporting omitted series honestly.
        let series:Vec<_>=rows.iter().take(128).map(|row|serde_json::json!({"labels":row["metric"],"values":row["values"]})).collect();
        Ok(serde_json::json!({"metric":metric,"host":host,"start":start,"end":end,"step":step,"series":series,"truncated":rows.len()>128}))
    }).await.map_err(|_|"Lecture interrompue".to_string())?
}

#[cfg(test)]
mod metric_tests {
    use super::*;
    #[test]
    fn only_catalogued_queries_and_hosts_are_accepted(){
        assert!(metric_query(999,"all",100,200).is_err());
        assert!(metric_query(1,"all\"} or up",100,200).is_err());
        assert!(metric_query(1,"all",200,100).is_err());
        assert!(metric_query(1,"all",1,400*86400).is_err());
        let (q,step)=metric_query(5,"prod",100,200).unwrap();
        assert!(q.contains("host=~\"prod\""));assert_eq!(step,15);
        assert!(metric_query(25,"prod",100,200).unwrap().0.contains("host=\"prod\""));
    }
}

#[tauri::command]
pub fn infrastructure_export_series(payload:serde_json::Value)->Result<String,String>{
    use std::io::Write;
    use std::os::unix::fs::DirBuilderExt;
    let host=payload["host"].as_str().ok_or("Machine absente")?;
    let metric=payload["metric"].as_u64().ok_or("Mesure absente")?;
    metric_query(metric,host,payload["start"].as_u64().unwrap_or(0),payload["end"].as_u64().unwrap_or(0))?;
    let series=payload["series"].as_array().ok_or("Séries absentes")?;
    if series.len()>128{return Err("Trop de séries".into());}
    let mut csv=String::from("timestamp_unix,machine,mesure,serie,valeur\n");
    for row in series{
        let labels=row["labels"].as_object().ok_or("Série invalide")?;
        let label=labels.iter().filter(|(k,_)|!matches!(k.as_str(),"__name__"|"job"|"instance")).map(|(k,v)|format!("{k}={}",v.as_str().unwrap_or(""))).collect::<Vec<_>>().join("; ");
        if label.len()>4096{return Err("Libellé trop long".into());}
        let label=label.replace('"',"\"\"").replace(['\r','\n']," ");
        let points=row["values"].as_array().ok_or("Valeurs absentes")?;
        if points.len()>300{return Err("Trop de points".into());}
        for point in points{
            let t=point[0].as_f64().filter(|x|x.is_finite()).ok_or("Date invalide")?;
            let value=point[1].as_str().and_then(|v|v.parse::<f64>().ok()).filter(|v|v.is_finite());
            if let Some(value)=value {csv.push_str(&format!("{t},{host},{metric},\"'{label}\",{value}\n"));}
            if csv.len()>8*1024*1024{return Err("Export trop volumineux".into());}
        }
    }
    let folder=std::path::Path::new("/home/ops-user/Documents/Ops-Exports");
    std::fs::DirBuilder::new().recursive(true).mode(0o700).create(folder).map_err(|_|"Dossier d’export indisponible")?;
    if !std::fs::symlink_metadata(folder).map_err(|_|"Dossier indisponible")?.is_dir(){return Err("Dossier non conforme".into());}
    let stamp=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|_|"Horloge invalide")?.as_nanos();
    let path=folder.join(format!("mesures-{host}-{metric}-{stamp}.csv"));
    let mut file=std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path).map_err(|_|"Création impossible")?;
    file.write_all(csv.as_bytes()).and_then(|_|file.sync_all()).map_err(|_|"Écriture impossible")?;
    Ok(path.to_string_lossy().into())
}
