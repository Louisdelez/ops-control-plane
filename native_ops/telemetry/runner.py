"""Load only root-owned installed telemetry modules in isolated Python mode."""
import runpy,sys
sys.path.insert(0,'/usr/local/libexec/ops-telemetry')
runpy.run_path('/usr/local/libexec/ops-telemetry/service.py',run_name='__main__')
