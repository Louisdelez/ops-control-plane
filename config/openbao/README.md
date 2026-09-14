# OpenBao

## État courant

OpenBao 2.6.2 est déjà initialisé, non scellé et audité en TLS/Raft sur
loopback. Deux parts Shamir chiffrées `host+tpm2` alimentent l'unité d'auto-unseal
et le token root initial a été révoqué. **Ne pas relancer l'initialisation sur
ce nœud.** Les étapes 1 à 4 ci-dessous sont conservées uniquement comme
procédure contrôlée pour une reconstruction sur une nouvelle installation
LUKS ; elles ne sont pas une action à exécuter sur le service courant.

## Reconstruction initiale uniquement

1. Creer trois identites PGP distinctes et sauvegarder leurs cles privees hors
   du Dell, sur des supports separes.
2. Depuis un terminal sans journalisation, initialiser en chiffrant chaque part
   et le token initial. Remplacer les quatre chemins par des cles publiques :

   ```bash
   bao operator init \
     -key-shares=3 \
     -key-threshold=2 \
     -pgp-keys=/media/share-a.pub,/media/share-b.pub,/media/share-c.pub \
     -root-token-pgp-key=/media/operator-root.pub
   ```

   La sortie ne contient alors que des blobs PGP chiffres. La conserver hors
   du Dell, jamais dans ce depot ni dans son historique de terminal.
3. Hors ligne, dechiffrer deux parts sur leurs supports respectifs, puis les
   saisir une par une dans le prompt interactif `bao operator unseal`.
4. Dechiffrer le token initial uniquement pour la session de bootstrap et
   lancer `scripts/bootstrap-openbao.sh`. Le script verifie un login `ops-user`
   avec `-no-store`, puis revoque lui-meme le token initial encore en memoire.
   Choisir pour ce compte OpenBao une phrase de passe forte et distincte du
   mot de passe de session Fedora.
5. Avec une session humaine ephemere, renseigner les champs exacts suivants par
   entree standard (jamais dans un argument ou l'historique) :

   - `kv-infra-shared/llm/deepseek`, champ `api_key` ;
   - `kv-infra-shared/hermes/gateway`, champ `api_server_key`.

   La cle de passerelle doit etre une valeur aleatoire d'au moins 128 bits et
   les deux valeurs doivent rester dans l'alphabet sûr
   `A-Za-z0-9._~+/=-`. La cle DeepSeek n'etant pas presente sur cette machine,
   cette etape ne peut pas etre executee maintenant. Procedure sans valeur
   dans les arguments, l'historique ou un fichier token :

   ```bash
   set -Eeuo pipefail
   set +x
   ops_bao_token=''
   ops_deepseek_key=''
   trap 'if [[ -n ${ops_bao_token:-} ]]; then BAO_TOKEN="$ops_bao_token" bao token revoke -self >/dev/null 2>&1 || true; fi; unset ops_bao_token ops_deepseek_key' EXIT
   read -r -s -p 'OpenBao password: ' ops_bao_password; echo
   ops_bao_token=$(printf '%s' "$ops_bao_password" | \
     bao login -no-store -token-only -method=userpass username=ops-user password=-)
   unset ops_bao_password
   read -r -s -p 'DeepSeek API key: ' ops_deepseek_key; echo
   [[ ${#ops_deepseek_key} -ge 16 && $ops_deepseek_key =~ ^[A-Za-z0-9._~+/=-]+$ ]]
   printf '%s' "$ops_deepseek_key" | BAO_TOKEN="$ops_bao_token" \
     bao kv put -mount=kv-infra-shared llm/deepseek api_key=-
   unset ops_deepseek_key
   openssl rand -base64 48 | tr -d '\n' | BAO_TOKEN="$ops_bao_token" \
     bao kv put -mount=kv-infra-shared hermes/gateway api_server_key=-
   BAO_TOKEN="$ops_bao_token" bao token revoke -self >/dev/null
   unset ops_bao_token
   trap - EXIT
   ```
6. Une fois l'organisation et le bot Zulip crees, renseigner un unique objet
   KV v2 `kv-infra-shared/zulip/bot` depuis une session humaine ephemere sans
   journalisation. Ne mettre aucune valeur dans la ligne de commande ni dans
   un fichier persistant. L'objet doit contenir exactement les champs requis
   suivants :

   ```text
   realm_url, bot_email, api_key, bot_user_id, approver_user_ids
   approval_stream, approval_stream_id, approval_topic
   alert_stream, alert_stream_id, alert_topic
   daily_stream, daily_stream_id, daily_topic
   ```

   Les seuls champs optionnels admis sont `ca_bundle`,
   `poll_timeout_seconds`, `retry_max_seconds`, `producer_poll_seconds` et
   `pending_page_size`. Les identites humaines et les streams sont toujours
   des IDs numeriques verifies, jamais des noms seuls. Cette etape n'est pas
   executable maintenant : le realm, le bot et les IDs Zulip n'ont pas encore
   ete fournis.
7. Centraliser la clé Qdrant et émettre l’identité AppRole de la mémoire :

   ```bash
   sudo systemctl stop ops-memory.service ops-memory-qdrant.service \
     ops-memory-secrets.service ops-memory-maintenance.service \
     ops-memory-maintenance.timer ops-memory-health.service ops-memory-health.timer
   sudo /usr/local/sbin/provision-memory-openbao
   ```

   L’outil réconcilie la policy `ops-memory-runtime`, limitée au GET exact de
   `kv-infra-shared/data/memory/qdrant` et à `revoke-self`. Son token dure 60
   secondes et possède exactement deux usages. Le provisionneur migre sans
   affichage l’ancien blob Qdrant si présent, teste login/lecture/révocation et
   persiste uniquement RoleID et SecretID sous forme `host+tpm2`. La clé reste
   uniquement dans OpenBao et sa copie d’exécution uniquement dans `/run`.
   L’absence de TPM, une divergence des deux clés ou un test AppRole incomplet
   fait échouer la transaction sans démarrer de service.
8. Installer ou verifier Hermes, puis lancer le provisionneur post-bootstrap :

   ```bash
   sudo ./scripts/install-hermes.sh
   sudo /usr/local/sbin/provision-hermes-openbao
   ```

   L'installateur pose d'abord `hermes-runtime.hcl`, `hermes-coordinator.hcl` et
   `deepseek-client.hcl` dans
   `/usr/local/share/ops-control-plane/openbao/policies`, exactement identiques
   aux sources revues, en `root:root 0644` et sans symlink. Les deux fichiers
   legacy sont des quarantaines `revoke-self` uniquement, sans lecture de clé.

   Le provisionneur exige ces trois fichiers installés avec leur contenu,
   propriétaire et mode exacts. Il demande ensuite le mot de passe OpenBao
   `ops-user` et, avec ce token humain, réapplique la policy `hermes-runtime`
   minimale et son rôle à deux usages. Avant toute rotation, il remplace aussi
   les policies `deepseek-client` et `hermes-coordinator` par leur quarantaine,
   supprime les deux anciens AppRoles et vérifie leur absence. Si l'API de
   suppression ou sa vérification échoue, la migration reste fail-closed :
   Hermes demeure arrêté, aucun nouveau SecretID n'est émis et le message
   indique les deux chemins AppRole à supprimer dans une session humaine
   approuvée.

   Il controle ensuite les deux tokens locaux, emet un SecretID a TTL de 30
   jours, teste login/read/revoke, puis chiffre RoleID, SecretID et accessor
   avec `systemd-creds --with-key=host+tpm2`. Aucun de ces identifiants n'est
   affiche ni ecrit en clair. Une rotation revoque l'ancien accessor. Le service
   reste desactive et n'est jamais demarre par ce provisionneur.
9. Installer le bridge Zulip puis, seulement apres l'etape 6, emettre son
   SecretID chiffre :

   ```bash
   sudo ./scripts/install-zulip-bridge.sh
   sudo /usr/local/sbin/provision-zulip-openbao
   ```

   L'AppRole `zulip-bridge` est lie a `127.0.0.1/32`, sans policy par defaut.
   Ses tokens durent 60 secondes, sont limites a deux usages (un GET exact et
   `revoke-self`) et ne sont jamais transmis au bridge. Son SecretID expire
   apres 30 jours et 1024 logins. Le provisionneur teste la lecture/revocation,
   chiffre RoleID, SecretID et accessor avec `host+tpm2`, puis laisse toutes les
   units desactivees. Le mtime de
   `/etc/credstore.encrypted/zulip-openbao-secret-id` date localement la
   rotation : renouveler avant J+30, idealement des l'alerte J+25.
10. Tester les refus croises entre projets avant toute activation d'agent.

## Atlas et rotation des clés fournisseur

Le catalogue source expose 58 cartes et 21 comptes fournisseur. Chaque compte
possède une unique `credential_ref` OpenBao ; les cartes qui utilisent le même
compte ne dupliquent donc jamais la clé. Les 21 correspondances
`provider_account_id` → `credential_ref` sont déclarées dans
`catalog/model-catalog.v2.json` et recopiées dans une allowlist fermée du helper.
Le test du dépôt exige que les deux listes restent exactement identiques :
ajouter une carte ne crée jamais implicitement un droit d'écriture.

Atlas ne collecte aucun secret dans son WebView. Il transmet seulement un
identifiant de compte borné au backend Rust, qui peut lancer exclusivement :

```text
/usr/local/libexec/ops-model-key-manager set <provider_account_id>
```

La copie installée doit être un fichier régulier `root:root`, non modifiable par
le compte desktop et sans symlink ; elle s'exécute comme `ops-user`. Elle demande
le mot de passe OpenBao puis la nouvelle clé avec `systemd-ask-password`, se
connecte uniquement à `https://127.0.0.1:8200` avec la CA locale, lit la version
KV v2, écrit le seul champ `api_key` avec CAS, puis révoque le token humain. Une
lecture de métadonnées, une écriture ou une révocation incomplète échoue fermée.
Le helper ne possède aucune commande de lecture, de copie ou d'affichage d'une
clé.

Cette rotation modifie la valeur stockée dans OpenBao, puis appelle exactement
le helper root sans argument `ops-model-credentials-reload`. Celui-ci ne lit
aucune valeur de clé et ne contacte aucun fournisseur : il sérialise avec le
déploiement, relance le résolveur local et contrôle seulement les métadonnées
des fichiers publiés. Il active la façade et Hermes si Qwen ou DeepSeek est
configuré, et le timer de solde si une clé financière revue est disponible ;
sinon ces consommateurs restent dormants. Le helper de saisie ne rapporte le
statut `rotated` qu'après les healthchecks locaux ; une clé ajoutée dans Atlas
est donc visible sans reboot. Aucun canari réseau n'est effectué. Seuls les
14 cartes et 16 déploiements issus des contrats API et identifiants exacts
revus peuvent devenir invocables ; stocker une clé pour l'un des autres comptes
ne suffit pas à lever son motif de blocage catalogue. L'installation des helpers
et de l'application reste une opération séparée, soumise au workflow
d'exploitation autorisé : la présence de leurs sources dans le dépôt ne signifie
pas qu'Atlas est déployé.

## Sauvegarde Raft

La sauvegarde quotidienne installée utilise l'identité `openbao-backup` et un
AppRole sans policy par défaut. Son jeton de cinq minutes possède exactement
deux usages : GET du snapshot Raft puis `revoke-self`. Le snapshot HTTPS est
envoyé directement dans `age`; aucun snapshot brut n'est écrit sur disque. Les
artefacts chiffrés, leur SHA-256 et leur manifeste ont une rétention locale de
14 générations et publient des métriques non sensibles.

La clé privée `age` et une copie hors site doivent rester hors du Dell. Toute
restauration se teste sur une machine ou VM isolée selon
[le runbook](../../runbooks/openbao/openbao-raft-backup.md), jamais sur le nœud
actif et jamais avec `-force`.

Ne jamais fournir une part Shamir dans un argument, une variable persistante,
Zulip, un prompt LLM ou un fichier de ce depot.
