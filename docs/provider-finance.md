# Registre et soldes fournisseurs

L’orchestrateur charge au démarrage le registre public
`provider-integrations.v1.json`. Le chargement est strict : fichier régulier non
modifiable par le groupe ou les autres, taille limitée, schéma v1 exact,
21 comptes revus, références de secrets distinctes et URLs HTTPS validées. Les
références OpenBao ne sont jamais renvoyées par l’API.

## API locale

- `GET /v1/provider-integrations` renvoie les informations publiques, les
  déploiements connus et un `finance_state` de forme fixe pour chaque compte.
- `POST /v1/provider-finance/refresh` accepte exactement
  `{"project_id":"infra-shared","provider_account_id":"deepseek"}`. La valeur
  du compte peut aussi être `all`. L’autorisation vient de l’identité Unix du
  pair, comme pour les autres mutations de l’orchestrateur.

Commandes équivalentes :

```text
ops-orchestrator provider-integrations
ops-orchestrator refresh-finance --account all --project-id infra-shared
```

## États financiers

- `ok` : une réponse conforme au schéma officiel a été normalisée et conservée ;
- `credential_unavailable` : aucun fichier de credential sûr n’est disponible ;
- `provider_unavailable` : échec HTTP, limite ou transport sans corps exposé ;
- `unsupported_schema` : le fournisseur a changé de réponse ou aucun parseur
  revu n’existe ;
- `requires_admin_credential` : API administrative/cloud volontairement non
  appelée avec la clé d’inférence ;
- `console_only` : information disponible uniquement dans la console ;
- `unsupported` : pas d’API de solde applicable ou vérifiée ;
- `never_refreshed` : connecteur direct disponible mais pas encore exécuté.

Les connecteurs de solde direct sont limités aux URLs littérales officielles de
DeepSeek, Moonshot/Kimi et StepFun. Ils utilisent GET + Bearer, désactivent les
proxies et redirections, plafonnent le délai à 10 secondes et la réponse à
65 536 octets. Les réponses sont parsées champ par champ. DeepSeek conserve
séparément USD et CNY, ainsi que son booléen de disponibilité explicite.
Moonshot et StepFun n’indiquent ni devise ISO ni booléen de disponibilité dans
leur réponse documentée : ces champs restent donc `null`. Aucune conversion ni
déduction n’est faite.

Les appels distants sont isolés dans
`ops-orchestrator-provider-finance-daemon.service`, derrière le socket privé
`/run/ops-orchestrator-finance/api.sock`. Le worker stateless s'exécute sous
l'UID/GID dédié `opsfinance`; le répertoire est `0750 opsfinance:opsfinance` et
le socket `0660 opsfinance:opsfinance`. Il vérifie `SO_PEERCRED` et refuse tout
UID autre que `opsorchestrator`. Le daemon principal authentifie d'abord le
projet du client, puis transmet uniquement l'identifiant de compte ; aucune clé
ni réponse fournisseur brute ne traverse ce socket. Il revalide strictement la
réponse normalisée et lui seul l'enregistre dans SQLite avec un nouvel UUID et
un horodatage local. Le worker n'a ni `StateDirectory`, ni accès en écriture à
`/var/lib/ops-orchestrator`.

Le worker charge exactement les credentials DeepSeek, Moonshot et StepFun.
Les marqueurs systemd vides des deux comptes optionnels sont rejetés comme
credentials indisponibles avant tout accès réseau. L'orchestrateur principal
est le seul membre supplémentaire attendu du groupe `opsfinance`. La façade
Hermes utilise volontairement le même UID et le même ledger que le daemon
principal pour Qwen/DeepSeek : ce n'est donc pas une frontière de confiance
forte entre ces deux processus. En revanche son unité déclare
`InaccessiblePaths=/run/ops-orchestrator-finance`, et l'UID séparé du worker
empêche la façade comme le daemon principal de lire ses copies systemd des clés
Moonshot/StepFun.

| Processus | Credentials chargés |
| --- | --- |
| orchestrateur principal | Alibaba/Qwen, DeepSeek |
| worker finances | DeepSeek, Moonshot, StepFun |
| façade Hermes | Alibaba/Qwen, DeepSeek, token local de façade |
| client timer | aucun |

Les snapshots SQLite, persistés uniquement par le daemon principal après cette
seconde validation, sont immuables, limités en forme et en taille, interrogés
par lots de 200 maximum et plafonnés à 50 000 entrées par compte. Le timer
systemd demande un rafraîchissement toutes les 30 minutes via le socket Unix
public de l'orchestrateur ; le client du timer n’a accès ni au réseau IP ni aux
credentials. Le daemon principal relaie ensuite la demande autorisée au worker
financier privé.
