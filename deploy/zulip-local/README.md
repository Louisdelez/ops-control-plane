# Zulip local 12.2 — paquet de déploiement

Ce paquet fournit un Zulip auto-hébergé, limité à la machine locale, pour le
bridge Ops existant. Les tests et les modes `--check` ne déploient rien. La
cible est exclusivement **Docker Engine rootful + Docker Compose
plugin sur x86_64**. Le mode rootless n'est pas supporté par l'image officielle
Zulip et ce paquet le refuse explicitement.

## Contrat de sécurité

- Interface unique : `https://zulip.ops.local:8443`, publiée seulement sur
  `127.0.0.1`. Aucun port HTTP, SMTP, PostgreSQL, Redis, RabbitMQ ou memcached
  n'est publié sur l'hôte.
- Zulip Server `12.2-0` et ses quatre dépendances sont épinglés par les digests
  x86_64 fournis. `platform: linux/amd64` est imposé aux cinq services.
- Les quatre secrets Compose des dépendances, les sept secrets applicatifs
  (dont cinq secrets internes persistants de Zulip) et le
  matériel TLS proviennent de l'objet KV v2 `kv-infra-shared/zulip/server`. Le
  service de rendu les écrit uniquement dans le tmpfs hôte
  `/run/zulip-local`. Les secrets sont `0400`, sauf le mot de passe memcached
  `0444` requis par l'utilisateur non privilégié de l'image épinglée; leurs
  parents hôte restent root-only `0700`. Le fichier complet
  `zulip-secrets.conf` est `root:1000 0640` : GID 1000 est le groupe `zulip`
  dans l'image 12.2, mais ne peut pas traverser son parent `root:root 0700` sur
  l'hôte. Le placeholder vide du mot de passe bootstrap est également `0444`.
- Quand SELinux est actif, le renderer applique le type `container_file_t` aux
  seules sources sous `/run/zulip-local`. Les deux mounts nécessitant un
  relabel privé utilisent la syntaxe bind courte `:Z`, car la Mount API Docker
  utilisée par les binds longs et les secrets fichier ne relaie pas ce flag.
- L'image officielle exige `/data/zulip-secrets.conf`. Un bind mount de ce
  fichier complet vers `/run/zulip-local/zulip-secrets.conf` empêche qu'il soit
  conservé dans le volume Docker. Le service Zulip ne reçoit volontairement
  aucun secret Compose préfixé `zulip__` : l'entrypoint amont les appliquerait
  avec le remplacement atomique de `crudini`, incompatible avec un bind de
  fichier exact. Comme les onze clés sont déjà présentes, le générateur amont
  est un no-op. Pour RabbitMQ et Redis, les adaptations locales construisent
  des fichiers de configuration respectivement `rabbitmq:rabbitmq 0400` et
  `redis:redis 0400` sur des tmpfs privés de 1 Mio. Le fichier SASL de
  memcached et sa base de mots de passe sont eux aussi créés avec un umask
  `0077` sur un tmpfs privé de 1 Mio. Ces trois montages éphémères disparaissent
  à l'arrêt et évitent toute copie de secret dans la couche writable Docker,
  sans masquer les fragments `/etc/rabbitmq/conf.d` de l'image. Les secrets
  CSPRNG sont validés avant rendu et excluent les caractères qui permettraient
  de sortir des valeurs entre apostrophes du format RabbitMQ. Aucun mot de
  passe dérivé n'est donc conservé dans l'environnement, une ligne de commande
  ou l'overlay. La configuration Compose ne contient que des chemins
  `/run/secrets` et aucune valeur.
- L'AppRole `zulip-local-runtime` peut seulement lire l'objet serveur puis
  révoquer son jeton. L'AppRole `zulip-local-bootstrap` peut seulement
  créer/lire/remplacer l'objet exact `kv-infra-shared/zulip/bot`, puis révoquer
  son jeton. Les SecretID durent 30 jours; les jetons durent 120 secondes, sont
  limités en nombre d'utilisations et liés à `127.0.0.1/32`.
- Les RoleID, SecretID et accessors persistants sont chiffrés avec
  `systemd-creds --with-key=host+tpm2`. Un TPM2 fonctionnel est requis.
  Le health check devient volontairement rouge après 25 jours afin de laisser
  cinq jours pour réexécuter le provisionneur avant l'expiration des SecretID.
- Le mot de passe du propriétaire Zulip passe exclusivement par le
  `--password-file` officiel de `manage.py create_realm`. Si aucun fichier
  root-only n'est fourni, `getpass` utilise deux invites masquées, puis crée un
  contenu éphémère sous `/run`, visible uniquement dans le conteneur comme
  secret fichier non préfixé, qui est tronqué dans un `finally`. Le placeholder
  vide reste présent pour que le mount Compose soit stable. Le mot de passe
  n'entre jamais dans argv, l'environnement, `zulip-secrets.conf` ou les
  journaux.
- La clé du bot et les identifiants sont gardés en mémoire le temps du
  provisionnement puis écrits, avec compare-and-set, dans l'objet OpenBao exact
  déjà consommé par `zulip-openbao-launcher`. Ils ne sont jamais affichés.
- Les snapshots reprennent la vue `/data` officielle mais excluent explicitement
  `zulip-secrets.conf`, le TLS manuel et l'ancien chemin
  `etc-zulip/zulip-secrets.conf`; le tar est envoyé directement à `age`. Aucun
  tar en clair ni copie supplémentaire de ces secrets n'est créé. La
  restauration conserve ces exclusions en défense supplémentaire et utilise
  l'objet OpenBao autoritatif rendu dans `/run`.

## Provenance officielle

La topologie de `compose.yaml`, les noms des secrets et les commandes des
dépendances proviennent du tag officiel immuable `zulip/docker-zulip` `12.2-0`
(commit `9e1d3cf566f3fb67e168c93f29a642eda989f6b7`, également déclaré dans le
label OCI de l'image). Les adaptations locales sont listées dans le
commentaire en tête du fichier : pins de digest, architecture, HTTPS loopback,
réseaux, mounts `/run`, confinement SELinux et rotation des logs.

Le 5 septembre 2026, une inspection directe des cinq registres avec `skopeo
inspect` a confirmé que chaque couple tag/digest configuré se résout en
architecture `amd64`; le digest reste l'autorité immuable utilisée par Compose.

Références primaires :

- <https://github.com/zulip/docker-zulip/blob/9e1d3cf566f3fb67e168c93f29a642eda989f6b7/compose.yaml>
- <https://zulip.readthedocs.io/projects/docker/en/latest/how-to/compose-getting-started.html>
- <https://zulip.readthedocs.io/projects/docker/en/latest/how-to/compose-secrets.html>
- <https://zulip.readthedocs.io/projects/docker/en/latest/how-to/compose-backups.html>
- <https://zulip.readthedocs.io/projects/docker/en/latest/how-to/compose-ssl.html>
- <https://github.com/zulip/zulip/blob/12.2/zerver/lib/management.py>
- <https://www.rabbitmq.com/docs/4.2/configure>
- <https://github.com/docker-library/rabbitmq/blob/master/4.2/ubuntu/docker-entrypoint.sh>

## Objets OpenBao

L'objet serveur doit contenir exactement :

```text
avatar_salt, camo_key, shared_secret, zulip_org_id, zulip_org_key
postgres_password, memcached_password, rabbitmq_password, redis_password
secret_key, email_password
tls_ca_certificate, tls_certificate, tls_private_key
```

`avatar_salt`, `camo_key`, `shared_secret`, `zulip_org_id` et `zulip_org_key`
sont précisément les valeurs que `generate_secrets.py` 12.2 créerait sinon
dans `/data`; les externaliser évite leur rotation à chaque recréation du
tmpfs. `provision-openbao.py --initialize-server-secrets` génère toutes les
valeurs avec le CSPRNG du noyau et OpenSSL P-256 dans un répertoire éphémère
sous `/run`, valide le document, l'écrit dans OpenBao avec CAS, puis détruit les
fichiers temporaires. Une rotation TLS remplace la CA et le certificat
ensemble; il faut ensuite relancer le rendu, réinstaller la CA publique et
redémarrer Zulip/bridge.

L'objet bridge produit automatiquement respecte le schéma existant :

```text
realm_url, bot_email, api_key, bot_user_id, approver_user_ids
approval_stream, approval_stream_id, approval_topic
alert_stream, alert_stream_id, alert_topic
daily_stream, daily_stream_id, daily_topic, ca_bundle
```

Les réglages optionnels existants `poll_timeout_seconds`,
`retry_max_seconds`, `producer_poll_seconds` et `pending_page_size` sont
validés selon les mêmes bornes que le launcher puis préservés. Si aucun champ
n'a changé, le provisionneur n'ajoute pas de version KV inutile.

## Cadre d'exécution supervisé

Conformément à `AGENTS.md`, un agent Codex ne lance aucune des commandes
mutantes ci-dessous directement. Installation, modification OpenBao/CA/hosts,
activation, backup, restauration et rollback passent par une mission/action du
broker, un runbook retourné par `list_authorized_runbooks` et, pour toute
classe C, une approbation Zulip enregistrée séparément. Le mot de confirmation
`RESTORE-ZULIP-LOCAL` n'est qu'un garde-fou local et ne constitue jamais cette
approbation. Les commandes suivantes sont une référence opérateur et les
primitives déterministes destinées aux runbooks.

## Référence d'installation

Depuis la racine du dépôt :

```bash
sudo bash deploy/zulip-local/bin/install.sh --check
sudo bash deploy/zulip-local/bin/install.sh --install
sudo /opt/ops-control-plane/deploy/zulip-local/bin/provision-openbao.py \
  --initialize-server-secrets
sudo systemctl start zulip-local-secrets.service
sudo /opt/ops-control-plane/deploy/zulip-local/bin/init.sh --start
sudo /opt/ops-control-plane/deploy/zulip-local/bin/provision-zulip.py
sudo systemctl enable --now zulip-local.service \
  zulip-local-health.timer zulip-local-backup.timer
```

Le premier `provision-openbao.py` demande le mot de passe du compte OpenBao
humain et une confirmation textuelle. Il installe les policies/roles, chiffre
les AppRole et ne démarre rien. `init.sh` installe la CA publique dans le trust
store et ajoute `127.0.0.1 zulip.ops.local` uniquement si le nom n'est pas déjà
résolu; il refuse un mapping existant non-loopback.

Pour fournir le mot de passe propriétaire via fichier :

```bash
sudo install -m 0400 -o root -g root /chemin/tmpfs/mot-de-passe \
  /run/zulip-owner-password
sudo /opt/ops-control-plane/deploy/zulip-local/bin/provision-zulip.py \
  --admin-password-file /run/zulip-owner-password
sudo rm /run/zulip-owner-password
```

### Premier accès propriétaire

Après le succès de `provision-zulip.py`, ouvrir
<https://zulip.ops.local:8443>, choisir la connexion par adresse email, puis
utiliser `admin@ops.local` et exactement le mot de passe fourni dans le fichier
éphémère (ou aux deux invites masquées). Le provisionneur ne génère et
n'affiche aucun mot de passe récupérable : l'opérateur doit donc conserver ce
mot de passe dans son gestionnaire de secrets avant de supprimer le fichier
temporaire. La politique locale accepte 12 à 100 caractères sans caractère de
contrôle, conformément à la limite maximale de Zulip 12.2.

Le script réconcilie sans duplication le realm `Ops`, le propriétaire, le bot
`ops-bot@zulip.ops.local`, et les canaux `Operations`, `Alerts`, `Daily Reports`.
Il utilise les endpoints serveur officiels `/api/v1/bots`,
`/api/v1/users/me/subscriptions`, `/api/v1/get_stream_id` et `/api/v1/users`.

## Bridge existant

Le provisionnement ci-dessus dépose automatiquement le document exact attendu
par `scripts/zulip-openbao-launcher`. `install-ca.sh` installe sa valeur
`ca_bundle` à
`/etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt`. L'identité AppRole du
bridge lui-même reste gérée par le mécanisme déjà audité :

```bash
sudo /usr/local/sbin/provision-zulip-openbao
sudo systemctl enable --now zulip-alertmanager-query.socket \
  zulip-approval-bridge.service
```

Cette dernière activation échoue volontairement si le broker Ops, le proxy
Alertmanager, les credentials chiffrés ou l'objet bot sont incomplets.

## Accès

Sur l'hôte : <https://zulip.ops.local:8443>.

Depuis une autre machine, aucun port réseau n'est exposé. Utiliser un tunnel SSH
local explicite, installer la CA publique sur le client, et faire résoudre
`zulip.ops.local` vers l'extrémité locale du tunnel. L'exposition LAN/Internet
et un reverse proxy sont hors périmètre de ce profil local-only.

## Sauvegarde, restauration et rollback

La sauvegarde suit la commande officielle `app:backup`, puis le pattern officiel
de snapshot du volume, chiffré en flux avec le destinataire public age déjà
configuré pour la récupération OpenBao :

```bash
sudo /opt/ops-control-plane/deploy/zulip-local/bin/backup.sh
```

Chaque `.age` reçoit un `.sha256`. Aucun nettoyage de rétention destructif n'est
automatique.

Une restauration détruit les quatre volumes nommés existants. Elle exige le mot
de confirmation exact, vérifie le checksum, déchiffre une première fois en flux
pour contrôler chemins/types/tailles et présence d'un dump officiel, puis
restaure et lance un health check. Elle exige une pile active et gérée par
`zulip-local.service`; après le début destructif, tout échec arrête les
conteneurs partiels et laisse le bridge arrêté. Les répertoires parents du
snapshot et des deux fichiers d'entrée doivent être root-only ou au minimum
non modifiables par groupe/autres, afin d'interdire leur substitution entre
validation et utilisation :

```bash
sudo install -m 0400 -o root -g root /media/offline/identity.txt \
  /run/zulip-restore-identity
sudo install -m 0400 -o root -g root /media/offline/new-owner-password \
  /run/zulip-owner-password-after-restore
sudo /opt/ops-control-plane/deploy/zulip-local/bin/restore.sh \
  --backup /var/backups/zulip-local/zulip-local-YYYYMMDDTHHMMSSZ.tar.gz.age \
  --identity-file /run/zulip-restore-identity \
  --admin-password-file /run/zulip-owner-password-after-restore \
  --confirm RESTORE-ZULIP-LOCAL
sudo rm /run/zulip-restore-identity /run/zulip-owner-password-after-restore
```

Le fichier contient le nouveau mot de passe souhaité après restauration; il
n'est pas nécessaire de connaître celui du snapshot. Le script l'envoie par
stdin aux deux invites natives de la commande officielle `change_password`,
sans argv/environnement/log, puis réconcilie obligatoirement le bot, les canaux,
les identifiants et la clé API dans OpenBao. Une restauration ne peut donc pas
être déclarée réussie avec un objet bridge devenu incohérent.

Si le bridge était actif, la restauration l'arrête avant la destruction de la
base, conserve le verrou de cycle de vie pendant la réconciliation OpenBao,
puis le redémarre avec sa nouvelle clé avant de déclarer le succès.

`rollback.sh` appelle exactement ce workflow de restauration. Son seul mode
non destructif est `rollback.sh --stop-only`, qui arrête les conteneurs sans
supprimer les volumes.

## Limites honnêtes

- La saisie du mot de passe initial du propriétaire ne peut pas être entièrement
  non interactive sans qu'un opérateur fournisse un `password-file`; générer et
  afficher un mot de passe violerait le contrat de non-exposition.
- La clé privée age de restauration doit venir d'un support opérateur et être
  placée temporairement sous `/run`. La conserver sur le serveur avec les
  sauvegardes annulerait leur séparation de confiance.
- Le snapshot Zulip et l'objet serveur OpenBao forment ensemble le jeu de
  restauration. Le snapshot ne remplace pas la sauvegarde/récupération OpenBao.
- L'AppRole du bridge existant nécessite son provisionneur audité séparé; le
  lancer automatiquement réutiliserait ou transmettrait abusivement une
  identité humaine OpenBao.
- Zulip 12.2 ne fournit ni commande `create_bot` ni opération de création de
  bot dans son OpenAPI publié. Le serveur officiel expose toutefois la route
  utilisée par son UI (`POST /api/v1/bots`, dans `zproject/urls.py` et
  `zerver/views/users.py`); le provisionneur épinglé 12.2 l'utilise et la teste.
  Toute montée de version devra revalider explicitement ce contrat interne au
  lieu de prétendre à une garantie de compatibilité API inexistante.
- Les notifications email sortantes ne sont pas configurées dans ce profil
  local. `email_password` reste néanmoins initialisé dans la configuration
  applicative complète afin qu'une future configuration SMTP revue ne parte
  pas d'une valeur vide.
- Les commandes ont été préparées et testées statiquement dans ce dépôt; ce
  changement ne constitue pas un déploiement ni une preuve d'état de l'hôte.
