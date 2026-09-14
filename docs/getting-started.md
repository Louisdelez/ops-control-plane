# Démarrage et développement

## Prérequis

Le projet cible Linux avec systemd. Le bureau a été développé pour Fedora, GTK3 et WebKitGTK. L’installation universelle sur une machine neuve reste à valider. Utiliser une VM ou une machine de développement pour adapter les configurations.

Les scripts d’installation, bootstrap, publication et récupération peuvent modifier le système. Lire leur code et leurs paramètres avant toute exécution privilégiée.

## Python

Prévoir Git et Python 3.11 ou supérieur avec `venv`. Depuis la racine :

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e './broker[dev,mcp]' -e './memory[dev,mcp]' -e './orchestrator[dev,mcp]' -e './bridges/zulip[dev]' -e './provider-discovery[dev]' -e ./native_ops
```

Les dépendances de la PWA sont dans `apps/approvals/requirements.txt`. OpenBao, Qdrant et Zulip sont des services externes à ces paquets.

## Tests

Lancer les suites séparément pour éviter les collisions entre modules portant le même nom :

```sh
python -m pytest -q broker/tests
python -m pytest -q memory/tests
python -m pytest -q orchestrator/tests
python -m pytest -q bridges/zulip/tests
python -m pytest -q provider-discovery/tests
PYTHONPATH=native_ops:orchestrator/src python -m pytest -q native_ops/tests
```

Certaines suites vérifient des fichiers de déploiement et supposent Linux. Les scripts `live_*`, tests de navigateur et recettes de restauration ont leurs propres prérequis et ne sont pas des tests unitaires. Ne pas leur fournir de vrais secrets sans avoir revu leur comportement.

## Bureau Rust/Tauri

Prévoir Rust/Cargo compatible avec les manifestes, un compilateur C/C++, `pkg-config`, OpenSSL, GTK3, WebKitGTK 4.1 et librsvg. Les paquets Fedora employés par le projet comprennent `gcc-c++`, `pkgconf-pkg-config`, `openssl-devel`, `gtk3-devel`, `webkit2gtk4.1-devel` et `librsvg2-devel`.

```sh
cd apps/ops-desktop/src-tauri
CARGO_BUILD_JOBS=2 cargo build --release --locked
cargo test --release --locked
```

La compilation ne configure pas les services affichés dans les onglets. Lire la [documentation du bureau](../apps/ops-desktop/README.md) et la [provenance du correctif glib](../apps/ops-desktop/vendor/README.md).

## Préparer un laboratoire

1. Revoir les politiques du broker et les runbooks.
2. Adapter utilisateurs Unix, projets, chemins, sockets et hôtes.
3. Configurer OpenBao et les identités de service avec des droits minimaux.
4. Tester broker et mémoire, y compris les refus d’accès.
5. Raccorder Zulip et le connecteur ; tester interruptions et approbations.
6. Activer les fournisseurs nécessaires avec leurs plafonds.
7. Reconstruire et vérifier les manifestes avant un déploiement privilégié.

`inventory/ops-v1.json` est un exemple avec gestion et découverte désactivées. Les chemins `/home/ops-user` et domaines `example.org` sont fictifs. Les anciens scripts de migration servent de références et de fixtures ; ils ne forment pas un installateur générique.
