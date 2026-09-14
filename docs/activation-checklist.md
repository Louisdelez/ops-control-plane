# Checklist d'activation externe et production

Le socle local historique est installé. Le nouveau catalogue, Atlas et la
configuration API-first sont présents dans les sources, mais ne sont pas
déployés par cette seule présence. Cette checklist ne décrit que les opérations
qui ne peuvent pas être terminées honnêtement sans présence physique, compte
tiers, clé API, workflow d'exploitation autorisé ou inventaire de production.
Ne jamais placer un secret dans ce dépôt, Zulip, un argument de commande ou un
prompt LLM.

La campagne NVIDIA/Ollama et son benchmark thermique appartiennent désormais à
l'historique. Aucun modèle local ne doit être promu ni réintroduit dans la
configuration runtime par défaut. Conserver les rapports existants comme
preuves et planifier séparément la désactivation des unités devenues inutiles.

La transaction de bootstrap locale autorisée installe l'orchestrateur API-first,
Atlas, Hermes et le Zulip local sans exiger de compte ni de clé fournisseur. Le
résultat attendu avant ajout de clés est `healthy-degraded` : le catalogue, les
budgets, l'approbation et les interfaces locales fonctionnent, tandis que tout
appel payant reste fermé avec `credential_unavailable`. Les éléments ci-dessous
concernent donc l'activation ultérieure de fournisseurs ou de cibles externes,
pas l'achèvement du socle local.

## 1. Prérequis physiques

- Sauvegarder le dépôt, les procédures et les éléments de récupération OpenBao,
  puis réinstaller Fedora avec LUKS avant d'introduire des secrets de production.
  La racine Btrfs actuelle n'est pas chiffrée et ne peut pas être convertie ici
  sans migration/réinstallation planifiée.
- Remplacer la batterie, dont la capacité utile constatée est d'environ 18 %, et
  prévoir un onduleur si le Dell doit servir de control plane 24/7.

## 2. Entrées externes à fournir

- selon les comptes choisis, une clé API DeepSeek, Alibaba/Qwen ou d'un autre
  fournisseur allowlisté, saisie uniquement dans Atlas ;
- pour Alibaba/Qwen, une clé compatible avec le point d'accès Global Singapore
  épinglé, avec accès aux modèles Qwen épinglés et à `deepseek-v4-flash` ; ce
  dernier partage cette clé et cet endpoint, sans
  troisième secret, et Qwen Global ne garantit pas à lui seul une résidence UE ;
- des plafonds de facturation durs configurés dans chaque compte fournisseur,
  en complément du ledger local ;
- facultativement, les endpoints et clés séparés des APIs d'embedding et de
  reranking ;
- l'authentification humaine du compte Claude Code (`claude auth login`) ;
- un nouveau pair WireGuard et des identités SSH/Windows séparées par rôle ;
- l'inventaire exact des hôtes, services, domaines, certificats, bases,
  sauvegardes et environnements à administrer.
- une clé publique `age` de sauvegarde dont la clé privée est conservée hors du
  Dell, plus une cible et une identité de transport hors site.

Faire tourner tout credential ancien avant usage et ne pas réutiliser une clé
retrouvée dans les exports d'audit.

## 3. OpenBao : état acquis et ajouts attendus

OpenBao est déjà initialisé, non scellé et audité. L'auto-unseal `host+tpm2` a
été recetté, le token root initial a été révoqué et la clé Qdrant est centralisée
dans OpenBao. `ops-memory-runtime` la lit au démarrage avec un jeton court puis
le révoque ; le Dell ne conserve que le RoleID et le SecretID chiffrés.

La sauvegarde Raft quotidienne locale est également active. L'identité
`openbao-backup` obtient par AppRole un jeton de cinq minutes à deux usages :
lecture du snapshot exact puis `revoke-self`. Le flux est chiffré directement
avec `age`, hashé, manifesté et retenu sur 14 générations, sans copie claire.
Elle ne devient une sauvegarde de récupération complète qu'après copie hors
site et conservation indépendante de la clé privée `age`.

Ne pas relancer une cérémonie d'initialisation. Conserver hors machine les parts
de récupération existantes et la clé privée `age`. Tester la restauration
uniquement sur une machine/VM isolée selon
[le runbook Raft](../runbooks/openbao/openbao-raft-backup.md), jamais sur le
nœud actif. Après une réinstallation LUKS, restaurer selon
[la procédure OpenBao](../config/openbao/README.md) et vérifier le compte humain
avant toute activation.

Le bootstrap crée les identités AppRole, le token local de façade et le realm,
le propriétaire, le bot et les canaux Zulip. Ces valeurs ne sont ni demandées
dans le chat ni copiées dans le dépôt. Quand une vraie clé fournisseur devient
disponible, l'ajouter ou la remplacer depuis l'onglet **Accès** d'Atlas : les
deux dialogues natifs collectent le mot de passe OpenBao puis la clé, et le
helper republie les credentials des seuls consommateurs fixes. Hermes ne reçoit
jamais directement une clé fournisseur. La disponibilité distante ne doit être
affirmée qu'après un canari borné et explicitement autorisé.

### Catalogue et Atlas

Avant l'installation, exécuter la section « Catalogue et application Atlas
dans les sources » de [la recette de vérification](verification.md). Elle doit
confirmer 58 cartes, 21 comptes, 48 cartes tarifées, 11 cartes en quarantaine et
le scénario commun de 10 M tokens d'entrée + 2 M de sortie.

Dans le workflow de classe C approuvé, l'installateur orchestrateur pose le
catalogue, sa preuve source et le helper
`/usr/local/libexec/ops-model-key-manager`. Vérifier que ce dernier est un
fichier régulier `root:root 0755`, sans symlink et non modifiable par `ops-user`.
Construire ensuite le RPM Atlas avec `Cargo.lock` et l'installer comme changement
séparé ; le build seul ne déploie rien.

Au premier lancement, confirmer que les 58 cartes restent consultables même si
le socket runtime est absent, et que les états « déploiement », « clé résolue »
et « canari réseau » restent alors inconnus. Le budget affiché vient du ledger
interne et ne doit jamais être présenté comme le solde réel du fournisseur. Une
rotation de clé doit mettre à jour OpenBao puis exécuter le rechargement local
fixe : Qwen ou DeepSeek peut activer la façade et Hermes, tandis que DeepSeek,
Moonshot ou StepFun peut activer le timer financier. Elle ne doit jamais lancer
un canari fournisseur. Enfin, ne valider comme invocables que les 14 cartes
reliées aux 16 déploiements runtime revus ; une clé, un déploiement déclaré ou
un tarif catalogue ne remplacent ni l'activation ni un canari réussi.

## 4. Zulip et approbations

1. Ajouter chaque approbateur sous la forme `zulip:<ID>` dans
   `policies/actions.yaml` et conserver exactement la même liste numérique dans
   le secret Zulip.
2. Réinstaller explicitement la policy revue, puis effectuer le preflight :

   ```bash
   sudo ./scripts/install-broker.sh --enable-api --replace-managed-configs
   sudo ./scripts/install-zulip-bridge.sh --check
   sudo ./scripts/install-zulip-bridge.sh --activate
   ```

3. Depuis un compte humain non bot, tester une réaction `✅` et une réaction
   `❌` sur le message, stream et sujet attendus.
4. Tester un utilisateur non autorisé : sa réaction doit être refusée et
   auditée, sans déclencher l'action.
5. Valider sur iPhone réception d'alerte et demande d'approbation, puis les six
   commandes bornées : `!ops mission`, `!ops statut`, `!ops reprendre`,
   `!ops répondre`, `!ops rapport` et `!ops capture`. Vérifier qu'une capture
   absente ou non conforme est refusée et qu'aucune commande mobile n'exécute
   directement une action.

## 5. Hermes et modes supervisés

Hermes 0.21 est installé avec huit profils, cinq skills et trois MCP locaux.
La cible API-first autorise 44 outils par profil (31 broker + 6 orchestrateur +
7 mémoire), parmi les 9 outils que le serveur orchestrateur publie. Dans ces
sources, son unique alias modèle
`qwen-coordinator` pointe vers la façade loopback budgétée de l'orchestrateur.
Cette façade appelle Qwen3.7 Flash et réconcilie chaque appel dans le ledger ;
Hermes ne reçoit aucune clé fournisseur et son egress est limité au loopback.
Les besoins non triviaux, incertains, de code ou risqués passent par l'outil MCP
`route_model_task`, qui applique les paliers Qwen/DeepSeek puis rend le contrôle
aux outils déterministes et à l'humain.

Le code, les unités et les profils sont prêts dans le dépôt, mais cette chaîne
n'est ni déployée ni activée par leur seule présence. Après approbation Zulip
durable de classe C, provisioning OpenBao, configuration de l'endpoint et des
caps, installer les defaults API-first et démarrer l'orchestrateur :

```bash
sudo /home/ops-user/ops-control-plane/scripts/install-orchestrator.sh \
  --replace-api-config --enable
```

Effectuer ensuite des canaris bornés distincts pour Qwen, DeepSeek hébergé par
Alibaba et l'API DeepSeek directe, puis la recette des refus de budget. Pour
`ROLE_LOCAL_OPS`, vérifier l'ordre DeepSeek Alibaba, Qwen3.7 Plus, puis DeepSeek
direct ; le provider Alibaba ne doit pas apparaître dans `ROLE_CODER`. La façade
est démarrée par la dépendance de la gateway ; activer Hermes seulement après
succès de ces contrôles :

Après satisfaction du preflight :

```bash
sudo install -o root -g root -m 0644 /dev/null /etc/hermes/hermes-gateway.enabled
sudo systemctl enable --now hermes-gateway.service
```

Codex est raccordé sous `codex-supervised`. Claude Code 2.1.236, installé depuis
le RPM officiel signé, est raccordé sous `claude-supervised`, mais l'opérateur
doit terminer l'authentification :

```bash
claude auth login
claude doctor
claude mcp list
```

Les modes supervisés utilisent le broker, l'orchestrateur et la mémoire ; ils ne
reçoivent ni shell root global ni accès direct aux secrets.

## 6. Production : activation cible par cible

- Ajouter le pair WireGuard du Dell par une demande de classe C sur
  l'infrastructure partagée, sans modifier les pairs existants.
- Créer des comptes Unix/Windows et clés SSH distincts par rôle, avec
  sudoers/ACL minimaux.
- Ajouter les cibles Prometheus, DNS/certificats, backups, Minecraft, proxies et
  bases une par une. Chaque cible doit avoir propriétaire, scope, timeout,
  budget, test de panne, backup validé et rollback avant activation.
- Vérifier une sauvegarde et une restauration en staging avant tout premier
  déploiement. Le backup local mémoire ne remplace pas les backups des cibles.
- Ne jamais interpréter la présence d'un hôte dans `inventory/` comme une
  autorisation de le modifier. Les inventaires de cibles sont actuellement vides.

## 7. Recette de mise en service

Exécuter [la recette de vérification](verification.md), puis confirmer :

- aucun listener du control plane hors loopback ;
- aucun provider Ollama dans la configuration runtime de l'orchestrateur ;
- Hermes ne connaît que la façade `127.0.0.1:8643`, ne possède aucune clé
  fournisseur et ne peut sortir du loopback ;
- chaque appel Qwen Flash de la façade apparaît dans le ledger, y compris un
  timeout ou une déconnexion classé `uncertain` ;
- routage API refusé sans `remote_allowed=true`, puis choix du fournisseur au
  plus faible coût estimé dans le rôle requis ;
- `ROLE_LOCAL_OPS` tente DeepSeek Alibaba avant Qwen3.7 Plus puis DeepSeek
  direct, tandis que `ROLE_CODER` reste inchangé ;
- budgets durs par provider et coût maximal de requête effectivement bloquants ;
- aucune unité inattendue en échec et aucun événement Audit perdu ;
- chaînes d'audit et sauvegardes broker/mémoire/OpenBao valides ;
- restauration mémoire éphémère réussie ;
- exercice de restauration Raft réussi hors ligne dans une VM isolée avec la
  clé privée `age` apportée temporairement ;
- AIDE propre ;
- rapport quotidien reçu dans Zulip ;
- alerte de test `firing`, puis `resolved`, reçue une seule fois ;
- action A autorisée, budget B borné à une tentative et action C impossible sans
  approbateur numérique autorisé ;
- mission complète staging : demande mobile, contexte mémoire, action, health
  check, rollback simulé, clôture et audit.
