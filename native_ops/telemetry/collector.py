"""Fixed Linux telemetry. Never read cmdline, environ, sockets or user files."""
import json,os,re,time,subprocess
from pathlib import Path

def read(path):return Path(path).read_text(errors='replace')
def safe(value):return re.sub(r'[^\w .:/@+()-]','?',str(value))[:128]
def collect():
 now=time.time();uptime=float(read('/proc/uptime').split()[0]);ticks=os.sysconf('SC_CLK_TCK');page=os.sysconf('SC_PAGE_SIZE')
 stat=read('/proc/stat').splitlines();cpus={r.split()[0]:list(map(int,r.split()[1:9])) for r in stat if re.match(r'^cpu\d* ',r)}
 mem={r.split(':')[0]:int(r.split()[1])*1024 for r in read('/proc/meminfo').splitlines() if len(r.split())>1 and r.split()[1].isdigit()}
 info=read('/proc/cpuinfo');model=next((r.split(':',1)[1].strip() for r in info.splitlines() if r.startswith(('model name','Hardware'))),'CPU')
 net=[]
 for row in read('/proc/net/dev').splitlines()[2:]:
  name,raw=row.split(':',1);name=name.strip();v=list(map(int,raw.split()));base=Path('/sys/class/net')/name
  try:speed=int(read(base/'speed').strip())
  except (ValueError,OSError):speed=None
  try:state=read(base/'operstate').strip()
  except OSError:state='unknown'
  net.append({'name':safe(name),'rx_bytes':v[0],'tx_bytes':v[8],'rx_errors':v[2],'tx_errors':v[10],'rx_dropped':v[3],'tx_dropped':v[11],'speed_mbps':speed if speed and speed>0 else None,'state':state,'physical':(base/'device').exists() or name.startswith(('eth','en'))})
 disks=[];seen=set()
 for row in read('/proc/self/mountinfo').splitlines():
  a,b=row.split(' - ',1);parts=a.split();fs,source,*_=b.split();mount=parts[4].replace('\\040',' ')
  if fs not in {'ext2','ext3','ext4','xfs','btrfs','zfs','vfat','exfat','ntfs','ntfs3','fuseblk','nfs','nfs4','cifs'}:continue
  if source in seen or mount.startswith(('/var/lib/docker/','/snap/','/boot/efi')):continue
  try:
   v=os.statvfs(mount);total=v.f_blocks*v.f_frsize
   if not total:continue
   seen.add(source);disks.append({'mount':safe(mount),'device':safe(source),'filesystem':fs,'total_bytes':total,'used_bytes':(v.f_blocks-v.f_bfree)*v.f_frsize,'available_bytes':v.f_bavail*v.f_frsize,'inodes_total':v.f_files,'inodes_free':v.f_ffree})
  except OSError:pass
 io=[]
 for row in read('/proc/diskstats').splitlines():
  v=row.split();name=v[2]
  if len(v)<14 or name.startswith(('loop','ram','zram')) or (Path('/sys/class/block')/name/'partition').exists():continue
  io.append({'name':safe(name),'read_bytes':int(v[5])*512,'write_bytes':int(v[9])*512,'busy_ms':int(v[12])})
 processes=[];count=0
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   raw=read(p/'stat');end=raw.rfind(')');v=raw[end+2:].split();count+=1
   processes.append({'pid':int(p.name),'name':safe(raw[raw.find('(')+1:end]),'state':v[0],'cpu_seconds':(int(v[11])+int(v[12]))/ticks,'rss_bytes':max(0,int(v[21])*page),'threads':int(v[17]),'start_ticks':int(v[19])})
  except (OSError,ValueError,IndexError):continue
 # Bounded projection; both CPU lifetime and memory consumers included, no arguments.
 chosen={p['pid']:p for p in sorted(processes,key=lambda p:p['rss_bytes'],reverse=True)[:100]}
 chosen.update({p['pid']:p for p in sorted(processes,key=lambda p:p['cpu_seconds'],reverse=True)[:100]})
 temperatures=[]
 for hw in Path('/sys/class/hwmon').glob('hwmon*'):
  try:
   label=safe(read(hw/'name').strip())
   for p in hw.glob('temp*_input'):
    value=float(read(p))/1000
    if -20<value<150:temperatures.append({'name':label+' '+p.name.split('_')[0],'celsius':value})
  except (OSError,ValueError):pass
 gpu=[]
 if Path('/usr/bin/nvidia-smi').exists():
  try:
   p=subprocess.run(['/usr/bin/nvidia-smi','--query-gpu=name,utilization.gpu,memory.used,memory.total,power.draw,power.limit,temperature.gpu','--format=csv,noheader,nounits'],capture_output=True,timeout=3)
   if p.returncode==0:
    for row in p.stdout.decode().splitlines()[:8]:
     v=[x.strip() for x in row.split(',')];number=lambda x:float(x) if re.fullmatch(r'\d+(\.\d+)?',x) else None
     if len(v)==7:gpu.append(dict(zip(['name','usage_pct','memory_used_mib','memory_total_mib','power_w','power_limit_w','temperature_c'],[safe(v[0]),*[number(x) for x in v[1:]]])))
  except (OSError,subprocess.TimeoutExpired):pass
 return {'schema_version':1,'sample_at':now,'uptime_seconds':uptime,'boot_id':read('/proc/sys/kernel/random/boot_id').strip(),'cpu':{'model':safe(model),'cores':len(cpus)-1,'ticks':cpus,'load':list(os.getloadavg())},'memory':{'total_bytes':mem.get('MemTotal',0),'available_bytes':mem.get('MemAvailable',0),'used_bytes':mem.get('MemTotal',0)-mem.get('MemAvailable',0),'swap_total_bytes':mem.get('SwapTotal',0),'swap_used_bytes':mem.get('SwapTotal',0)-mem.get('SwapFree',0)},'filesystems':disks[:48],'network':net[:64],'disk_io':io[:48],'processes':list(chosen.values()),'process_count':count,'process_limit':200,'temperatures':temperatures[:24],'gpus':gpu}
if __name__=='__main__':print(json.dumps(collect(),separators=(',',':'),allow_nan=False))
