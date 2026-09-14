# Atlas — guide opérateur

Atlas donne une vue simple du catalogue, des coûts comparables, des budgets
internes et des accès fournisseurs. C'est une console locale personnelle : elle
ne route pas elle-même un appel et ne contourne ni l'orchestrateur ni le broker.

## Ce qui est visible

- **Catalogue** : 58 cartes normalisées provenant des 59 entrées du seed et
  21 comptes fournisseur, avec recherche, filtres, description, spécialités,
  tarifs et barres 0–10. La fiche détaillée distingue contexte, langues,
  limitations, latence, prix cache, confiance et source ; une donnée absente
  du TXT est affichée comme non documentée, jamais estimée silencieusement. Les
  48 cartes disposent d'au moins une grille tarifaire ; les 11
  cartes en quarantaine restent visibles et non routables. Les états sont
  exactement 5 `configured`, 42 `catalogued` et 11 `quarantined`; les cinq
  cartes configurées référencent sept déploiements catalogue.
- **Routage** : prévisualisation consultative par capacités, spécialités et
  plafond de coût. Les contraintes sont d'abord éliminatoires, puis les
  candidats admissibles sont classés du moins cher au plus cher.
- **Coûts** : le même scénario pour tous les modèles, soit 1 000 appels mensuels
  de 10 000 tokens d'entrée et 2 000 de sortie — 10 M/2 M au total. Les modes
  pleins/creux restent distincts dans les cartes, les fiches et le comparateur.
  Le plus coûteux est sélectionné par défaut pour une comparaison conservatrice,
  et la fiche permet de choisir chaque mode. Les familles sans tarif exact ne
  reçoivent aucun prix inventé.
- **Accès et système** : comptes fournisseurs, état local des credentials,
  déploiements déclarés et consommation des budgets de l'orchestrateur.

Les notes 0–10 sont des *priors éditoriaux à faible confiance*, dérivés du texte
fourni. Elles ne constituent ni benchmark, ni garantie de fiabilité. Le ledger
enregistre coûts, durées et validations externes. L'ajustement adaptatif borné
réordonne les candidats d'un même palier sans jamais réécrire les cartes.

## Prix de carte et coût du déploiement

Le prix d'une carte vient de sa grille source et de son compte fournisseur
primaire. Il ne représente pas automatiquement le tarif du même modèle servi
par un autre hébergeur. DeepSeek V4 Flash illustre ce cas : la carte conserve
les prix DeepSeek directs, tandis que son déploiement Alibaba possède un coût
runtime calculé avec les tarifs Alibaba configurés.

Une prévisualisation purement catalogue affiche donc une base
`catalogue_card`. Une prévisualisation limitée au runtime classe les
déploiements réellement déclarés et disponibles sur leur
`comparison_cost_microusd`, puis expose le déploiement et le compte hébergeur
retenus avec la base `runtime_deployment`. Dans les deux cas, le point de
comparaison reste identique : 10 M tokens d'entrée et 2 M de sortie par mois.

## Lire correctement les statuts

Les quatre notions suivantes ne sont jamais interchangeables :

1. **Catalogué** : la carte existe et peut être comparée.
2. **Déploiement déclaré** : une configuration runtime revue référence ce
   modèle ; cela ne prouve pas qu'une clé existe.
3. **Clé résolue par le runtime** : les contrôles locaux ont trouvé activation,
   configuration et fichier credential. Cela ne teste pas Internet.
4. **Canari fournisseur réussi** : seul un appel distant explicitement autorisé,
   borné et facturable peut prouver DNS, TLS, authentification et disponibilité
   réelle du modèle.

Atlas affiche donc « réseau fournisseur non testé » tant qu'aucune preuve de
canari n'existe. Après une rotation confirmée, le helper root-owned republie les
credentials puis recharge les consommateurs locaux fixes. Il active la façade
et Hermes lorsqu'une clé Qwen ou DeepSeek est disponible, et le timer de solde
pour DeepSeek, Moonshot ou StepFun. Cette recharge ne lance aucun canari et ne
prouve donc toujours pas la disponibilité réseau du fournisseur.

## Ajouter ou changer une clé

1. Ouvrir la carte ou l'onglet **Accès** et choisir **Configurer** ou **Changer**.
2. Le formulaire graphique intégré à Atlas/Tauri demande le mot de passe
   OpenBao de `ops-user` dans un champ masqué.
3. Le même formulaire demande la nouvelle clé du compte fournisseur dans un
   champ masqué.
4. Le helper écrit uniquement le champ `api_key` dans le chemin OpenBao
   allowlisté, avec contrôle de version CAS, puis révoque son token.

Tauri lance uniquement
`/usr/local/libexec/ops-model-key-manager set-stdin <compte-allowlisté>` et
transmet une trame binaire `ATLASKEY1` sur un pipe anonyme privé : longueur
big-endian puis octets OpenBao, longueur puis octets de clé, suivis d’EOF. Le
mot de passe est limité à 14–100 caractères (400 octets UTF-8 maximum) et la
clé à 4096 octets ; leurs buffers sont effacés après usage.
Le helper refuse un terminal, un fichier, une trame incomplète ou tout octet
supplémentaire. Le mode `set` avec dialogues système reste disponible comme
compatibilité locale, sans être utilisé par l’interface Tauri.

Une clé appartient au **compte fournisseur**, pas à chaque modèle. Les cartes
Alibaba partagent donc le même accès. DeepSeek V4 Flash peut être servi par le
compte DeepSeek direct ou par le compte Alibaba ; leurs budgets, clés et états
doivent rester séparés.

Le catalogue possède 21 comptes allowlistés et Atlas peut demander une rotation
pour chacun. Le runtime source relie 5 cartes statiques et génère 9 liaisons
revues via Z.ai, OpenAI Responses et Moonshot : 14 cartes sur 58 disposent d'un
contrat d'invocation et 44 restent bloquées avec un motif explicite. Stocker une
clé ne suffit jamais à activer un compte, rendre une carte disponible ou prouver
un canari réseau.

Le helper ne sait ni lire, ni révéler, ni copier une clé. Le WebView contient
uniquement les champs masqués nécessaires à la saisie ; leurs valeurs sont
transitoires en mémoire et effacées immédiatement après leur remise au backend.
Elles ne sont jamais placées dans le stockage, les journaux, une URL, argv ou
l'environnement. Les permissions Tauri n'autorisent aucun plugin shell, HTTP
ou filesystem.

## Budget interne et solde fournisseur

La barre **Budget interne restant** vient du ledger SQLite et des plafonds de
l'orchestrateur. Elle protège l'exécution locale, mais ce n'est pas l'argent
restant chez Alibaba, DeepSeek ou un autre fournisseur.

Un **solde fournisseur** n'est affiché que si une API officielle et authentifiée
le confirme. Les connecteurs directs revus couvrent DeepSeek, Moonshot/Kimi et
StepFun ; Atlas peut les actualiser et affiche leurs devises telles que reçues,
sans conversion ni devise inventée. Les comptes nécessitant une identité
administrative/cloud, une console ou un schéma non revu gardent un état typé
indisponible. Les plafonds de facturation doivent aussi être définis dans les
consoles fournisseurs.

Les caps restent appliqués par déploiement et sont maintenant doublés de
plafonds durs partagés par compte (`hard_shared_cap=true`). Le ledger vérifie
atomiquement les appels, tokens et coûts par jour, mois et tâche avant tout appel
réseau. Cela reste un budget interne, distinct du solde fournisseur.

## Limites actuelles

- les 48 cartes tarifées sont comparables et les 11 cartes en quarantaine restent
  en quarantaine ; 14 cartes ont un contrat d'invocation source (5 statiques et
  9 générées), 44 sont bloquées avec un motif explicite, et aucune disponibilité
  réelle n'est affirmée sans clé, activation et canari ;
- les sept déploiements rattachés au catalogue n'incluent pas
  `qwen-coder-api` : ce Qwen Coder Flash est une voie auxiliaire revue dont
  l'identifiant exact n'existe pas dans le seed. Il ne doit être assimilé ni à
  Qwen3-Coder-Next, ni aux variantes 30B/480B ; les backups génériques embedding
  et reranker sont eux aussi déclarés explicitement hors catalogue ;
- la prévisualisation du catalogue est consultative et ne remplace pas la route
  opérationnelle, les règles de risque ou une approbation humaine ;
- la note libre de mission reste locale à l'écran : la prévisualisation actuelle
  utilise les seuils de capacités choisis explicitement, sans classification par
  un modèle ni coût caché ;
- les résultats validés et la médiane d'au moins cinq latences terminées sont
  agrégés par catégorie ; l'ajustement `adaptive-quality-latency-v2` est
  auditable et son effet total reste borné à ±15 %. Il ne change ni les
  capacités minimales exigées ni le catalogue brut ;
- l'état de clé d'un compte sans déploiement runtime reste non observable ;
- une rotation de clé recharge les consommateurs fixes et peut activer Hermes
  ou le timer financier selon le compte ; elle ne contacte ni ne teste aucun
  fournisseur.

## Déploiement contrôlé

Le tout premier démarrage de la release `.11` passe par
`scripts/enroll-atlas-trust-anchor`. Une boîte graphique affiche les empreintes,
Polkit réalise l'authentification locale, puis les snapshots déjà scellés sont
publiés atomiquement sous `/usr/local/lib/ops-control-plane/`. Une ancre déjà
présente est utilisée directement : le checkout utilisateur ne reçoit plus de
nouvelle exécution privilégiée. L'assistant Tauri prend ensuite en charge la
création des mots de passe et la reprise, y compris la finalisation sans secret
d'un rollback déjà terminal.

L'installateur orchestrateur pose le catalogue, sa preuve source et le helper
en `root:root` :

```bash
sudo /home/ops-user/ops-control-plane/scripts/install-orchestrator.sh
```

Cette commande ne doit être exécutée qu'au cours de la migration approuvée. Sans
`--enable`, elle ne démarre pas les services. L'application RPM est construite
et installée séparément selon
[`../apps/model-manager/README.md`](../apps/model-manager/README.md).
Tant que ce workflow d'exploitation autorisé et l'installation du paquet n'ont
pas été effectués, Atlas reste une application présente uniquement dans les
sources ; aucun écran, helper ou état runtime nouveau ne doit être supposé
déployé sur la machine.
