# Hermes 0.21 — profils Ops

Cette configuration cible le tag annote Hermes Agent `v2026.8.31` (`0.21.0`),
objet tag `6e8f8418e6378eb2617e4de074e13dedd091b8af`, commit pele
`29112bef099274229cadff79cdff7bf7b99c4b77`. Le tag n'est pas signe GPG ; le
verrouillage repose donc sur ces deux identifiants Git verifies exactement.
Elle décrit la cible API-first versionnée ; son installation et son activation
sur la machine restent un changement séparé de classe C.

Le profil par defaut possede seul l'API sur `127.0.0.1:8642`. Les profils
multiplexes ne lient aucun port. Chaque profil utilise trois processus MCP
`stdio` locaux : broker deterministe, orchestrateur de modeles et memoire. Il
n'existe aucun MCP reseau ni aucun outil de lecture de secret ou d'approbation.

| Profil | Broker | Orchestrateur | Memoire / pair Unix |
| --- | --- | --- | --- |
| `default` | `hermes-coordinator` | `hermes-coordinator` | `hermes-coordinator` / `hermesd` |
| `minecraft-ops` | `minecraft-monitor` | `minecraft-ops` | `minecraft-ops` / `minecraft-ops` |
| `infra-shared` | `infra-operator` | `infra-shared` | `infra-shared` / `infra-network` |
| `network-shared` | `infra-network` | `network-shared` | `network-shared` / `infra-network` |
| `monitoring-shared` | `monitoring-shared` | `monitoring-shared` | `monitoring-shared` / `ops-monitor` |
| `backup-shared` | `backup-shared` | `backup-shared` | `backup-shared` / `backup-agent` |
| `deploy-ops` | `deploy-ops` | `deploy-ops` | `deploy-ops` / `deploy-agent` |
| `security-ops` | `security-ops` | `security-ops` | `security-ops` / `security-audit` |

Les wrappers root-owned fixent l'identite et le scope projet. Les profils
secondaires passent par des regles sudo exactes vers leur compte technique ;
la memoire controle ensuite le pair Unix avec `SO_PEERCRED`. Le broker applique
son RBAC par projet, runbook, classe et capacite. L'orchestrateur refuse un
`project_id` hors de l'allowlist avant tout appel de modele ou enregistrement de
signal, puis filtre aussi les lectures de traces. Le daemon recoupe ce projet
avec le grant de l'identite Unix obtenue par `SO_PEERCRED`. Les listes
`tools.include` sont reduites par profil. La
gestion des skills est soumise a approbation et leur arborescence est root-owned
et en lecture seule. Un skill ne peut pas elargir les permissions MCP.

Le token de la facade locale et `API_SERVER_KEY` sont resolus a l'execution par
`/usr/local/libexec/hermes-secrets-env`; aucun secret n'est stocke ici. Le
preflight exige OpenBao sain et non scelle, la facade budgetee saine sur
`127.0.0.1:8643` et la cle de l'API locale. Il n'affiche jamais la sortie du
helper.

Le moteur d'amorcage de chaque profil est l'alias `qwen-coordinator`, servi par
la facade OpenAI-compatible locale de l'orchestrateur. La facade impose Qwen
Flash, `enable_thinking=false`, un maximum de sortie, les vrais paliers de prix
et le ledger durable. Tous les profils partagent un plafond de mission de
0,05 USD par jour UTC qui survit aux redemarrages et ne depend pas du champ
client `user`. Les taches non triviales passent ensuite par
`route_model_task`, qui choisit Qwen ou DeepSeek selon capacite, risque et cout.
Hermes ne recoit aucune cle fournisseur et son unite systemd ne peut joindre que
loopback (`IPAddressDeny=any`, `IPAddressAllow=localhost`).

Le helper est installe root-owned et accepte uniquement les credentials systemd
de `hermes-gateway.service`. Il se connecte a l'AppRole `hermes-runtime`, lit
un chemin KV exact, revoque son jeton temporaire, puis et seulement puis
emet des valeurs dotenv strictement bornees. Les RoleID, SecretID et accessor
sont persistes exclusivement sous forme `host+tpm2` dans
`/etc/credstore.encrypted`. Le processus multiplexe tourne sous `hermesd`, mais
chaque MCP secondaire adopte le compte technique fixe indique dans le tableau.
Les profils partagent encore le moteur Hermes et le token de facade locale : ce
cloisonnement MCP ne pretend donc pas etre une isolation de processus complete.

Le modele Ollama `qwen3:0.6b` est conserve uniquement comme fait historique. Il
annoncait 40 960 tokens, sous le minimum de 64 000 de Hermes 0.21, et a echoue
le benchmark reel fail-closed sur la garde thermique. Aucune configuration
Hermes ne l'utilise et il ne doit pas etre promu. La passerelle reste dormante
tant que les cles Qwen/DeepSeek, le token de facade
et des plafonds fournisseurs externes n'ont pas ete valides. Les provisionneurs
refusent les champs absents ; ils ne creent aucun faux secret.

Validation et installation :

```bash
./scripts/validate-hermes-config.py
# Validateur Codex local optionnel et non portable :
for skill in config/hermes/skills/*; do
  python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py "$skill"
done
sudo ./scripts/install-hermes.sh
```

Recette offline complete (aucun demarrage) :

```bash
TMPDIR=/var/tmp ./scripts/validate-hermes-config.py
TMPDIR=/var/tmp python3 -m unittest tests.test_hermes_secrets_env
TMPDIR=/var/tmp pytest -q orchestrator/tests/test_mcp.py
visudo -cf config/hermes/ops-broker-mcp-profile.sudoers
visudo -cf config/hermes/ops-local-mcp-profile.sudoers
shellcheck scripts/install-hermes.sh scripts/hermes-gateway-preflight \
  scripts/ops-broker-mcp-profile scripts/ops-orchestrator-mcp-profile \
  scripts/ops-memory-mcp-profile
```

L'installateur ne cree aucun credential, ne demarre et n'active rien. Apres la
ceremonie OpenBao, le provisionneur interactif verifie les deux tokens locaux, les
bornes de l'AppRole, effectue un vrai test login/read/revoke, puis chiffre le
RoleID, le SecretID et son accessor sans afficher leur valeur :

```bash
sudo /usr/local/sbin/provision-hermes-openbao
```

Il refuse toute rotation pendant que la passerelle tourne. L'activation reste
une decision explicite de l'operateur, uniquement apres la ceremonie API de
l'orchestrateur, le controle des plafonds Qwen/DeepSeek et l'approbation durable
du changement de classe C :

```bash
sudo install -o root -g root -m 0644 /dev/null /etc/hermes/hermes-gateway.enabled
sudo systemctl enable --now hermes-gateway.service
```

Retirer le marqueur puis arreter le service remet la passerelle en etat dormant.
