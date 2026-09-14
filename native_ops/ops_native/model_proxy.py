"""Mission-scoped entry to the existing budgeted API facade; no model implementation."""
import json,os,threading,uuid
from pathlib import Path
from ops_orchestrator.config import load_config
from ops_orchestrator.database import Database
from ops_orchestrator.budget import BudgetLedger
from ops_orchestrator.hermes_facade import (
    MultiProviderHermesFacade,HermesFacadeHandler,ThreadingHermesFacadeServer,FacadeResponse)
from .credentials import read_credential
from .execution import api_allowed

scope=threading.local()
def identity():
    return ('native-'+uuid.uuid4().hex,'native/mission/'+scope.mission)

class Handler(HermesFacadeHandler):
    @property
    def facade(self):
        return self.server.advanced if getattr(scope,'advanced',False) else self.server.facade
    def do_POST(self):
        if not api_allowed():
            self.close_connection=True
            self._send(FacadeResponse(403,'application/json',b'{"error":{"message":"API disabled by execution mode"}}'))
            return
        try:
            raw=self.headers.get('X-Ops-Mission-ID','')
            if str(uuid.UUID(raw))!=raw:raise ValueError()
            level=self.headers.get('X-Ops-Reasoning','standard')
            if level not in {'standard','advanced'}:raise ValueError()
        except ValueError:
            self.close_connection=True
            self._send(FacadeResponse(400,'application/json',b'{"error":{"message":"valid mission scope required"}}'))
            return
        scope.mission=raw;scope.advanced=level=='advanced'
        try:super().do_POST()
        finally:
            scope.mission=None;scope.advanced=False

class Server(ThreadingHermesFacadeServer):
    def __init__(self,normal,advanced):
        super().__init__(('127.0.0.1',18643),normal,max_request_workers=2)
        self.advanced=advanced;self.RequestHandlerClass=Handler

def main():
    os.umask(0o077)
    config=load_config(Path('/etc/ops-native-model/config.json'))
    database=Database(config.database_path);database.initialize()
    token=read_credential('ops-native-facade-token')
    target=Path('/run/ops-native-model/facade-token')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:f.write(token)
    token=''
    ledger=BudgetLedger.from_config(database,config)
    normal=MultiProviderHermesFacade.from_app_config(config,provider_ids=['native-deepseek-flash'],ledger=ledger,client_token_path=target,identity_factory=identity)
    advanced=MultiProviderHermesFacade.from_app_config(config,provider_ids=['native-deepseek-reasoning'],ledger=ledger,client_token_path=target,identity_factory=identity)
    server=Server(normal,advanced)
    try:server.serve_forever(poll_interval=0.5)
    finally:server.server_close()

if __name__=='__main__':main()
