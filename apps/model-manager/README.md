# Atlas — gestionnaire de modèles IA

Atlas est l'application desktop verticale de gestion du catalogue multi-modèles.
Elle est écrite en Rust avec Tauri v2 et une interface HTML/CSS/JavaScript
statique : aucun serveur web, framework Node ou plugin Tauri réseau/shell/filesystem
n'est nécessaire au runtime.

Le catalogue public est embarqué à la compilation depuis
`../../catalog/model-catalog.v2.json`. L'état et les budgets sont lus sur le
socket Unix local `/run/ops-orchestrator/api.sock`. Une absence de socket laisse
les 58 cartes consultables, mais tous les états runtime restent inconnus.
Le jeu courant contient 21 comptes fournisseur, 48 cartes tarifées et 11 cartes
en quarantaine. Il se répartit en 5 cartes `configured`, 42 `catalogued` et 11
`quarantined`; les cinq cartes configurées portent sept déploiements catalogue.
Toutes sont visibles. Le runtime source possède 5 cartes reliées statiquement
et génère 9 liaisons supplémentaires revues via Z.ai, OpenAI Responses et
Moonshot : 14 cartes sur 58 ont donc un contrat d'invocation, pour 16
déploiements catalogue. Les 44 autres restent bloquées avec un motif explicite.
Ces chiffres ne prouvent ni présence de clé, ni activation, ni canari réussi.
Le Qwen Coder Flash runtime est explicitement auxiliaire et hors catalogue, car
aucune carte du seed ne porte son identifiant exact ; Atlas ne le fait donc pas
passer pour l'une des trois cartes Qwen Coder cataloguées.

Quand une carte possède plusieurs grilles, Atlas les conserve toutes dans la
carte, la fiche et le comparateur. Le mode le plus coûteux du scénario commun
est le défaut conservateur ; la fiche permet de sélectionner explicitement une
autre grille, par exemple les heures creuses ou pleines de DeepSeek.

Les soldes officiels DeepSeek, Moonshot/Kimi et StepFun peuvent être actualisés
depuis Atlas. L'orchestrateur relaie seulement l'identifiant du compte à un
worker financier isolé ; le WebView, Atlas et le client du timer ne reçoivent
jamais de clé ni de réponse fournisseur brute.

Atlas distingue une carte cataloguée, un déploiement déclaré, une clé résolue
par le runtime et un canari réseau réussi. Le prix source de la carte n'est pas
forcément celui d'un hébergement alternatif du même modèle : lorsqu'un
déploiement runtime est sélectionné, son coût comparatif propre et son compte
hébergeur sont affichés. Le scénario commun reste 10 M tokens d'entrée et 2 M
de sortie par mois pour toutes les cartes.
Les fiches détaillées exposent aussi contexte, langues, limitations, latence,
prix cache et provenance. Les champs inconnus restent explicitement non
documentés ; en particulier, le seed ne fournit aucune latence mesurée ni aucun
tarif cache exploitable. Un enrichissement officiel séparément sourcé peut
cependant compléter ces champs sans modifier la copie archivée du TXT.

## Installation et reprise corrective graphiques

Quand le bootstrap local n'a pas encore été consommé, Atlas ouvre directement
un assistant Tauri en cinq étapes. Il présente les comptes locaux fixes, lance
le préflight, permet de créer les mots de passe OpenBao et Zulip dans des champs
masqués, affiche la release et ses empreintes, recueille la confirmation
explicite puis suit la transaction et son rollback éventuel. L'authentification
du système est fournie par la boîte de dialogue Polkit du bureau. Aucun terminal
ni compte Zulip préexistant n'est nécessaire.

Avant le premier lancement de cette release, `scripts/enroll-atlas-trust-anchor`
capture le manifeste, le helper, le lanceur et l'autorité dans cinq memfd
scellés, affiche leurs empreintes dans une boîte graphique, puis demande
l'authentification Polkit. L'installateur privilégié ne rouvre aucun chemin du
checkout : il publie ces octets en une seule version root-owned sous
`/usr/local/lib/ops-control-plane/atlas-api-zulip-2026.09.08.11`. Atlas est
ensuite relancé depuis cette ancre. Une ancre courante valide est toujours
utilisée directement, sans relire le checkout. La seule ancienne ancre `.11`
dont les trois SHA-256 sont codés en dur peut toutefois être réconciliée après
une nouvelle confirmation graphique et Polkit : elle est déplacée intacte dans
un historique root-owned avant la publication atomique des nouveaux octets.
Tout autre écart échoue fermé.

Le backend Rust transmet les deux mots de passe au helper revu par deux pipes
anonymes privés avec le framing binaire `ATLASBOOT1`. Les secrets ne sont placés
ni dans les arguments, ni dans l'environnement, ni dans un fichier, ni dans les
journaux. Les champs et références JavaScript sont vidés dès la remise au
backend; les buffers Rust sont protégés par `zeroize`. Le helper privilégié
vérifie à nouveau l'identité de la release, de la session graphique, du binaire
Atlas et des pipes avant la frontière de mutation.

Si Atlas trouve précisément le marqueur et le WAL `.10` dans leur état terminal
`rolled-back`, sans Atlas, Docker ni Zulip installés, l’assistant propose le
parcours **Vérifier et réparer** au lieu d’une nouvelle installation. Il refuse
toute autre origine ou combinaison partielle. Le préflight privilégié affiche
explicitement la transition `.10` vers `.11`; les quatre champs de création et
confirmation des mots de passe ne sont rendus disponibles qu’après cette preuve
root. Une annulation ou un échec vide immédiatement les champs, et une nouvelle
tentative exige une nouvelle saisie.

Le backend utilise alors les commandes neutres `gui-recovery-preflight` et
`gui-recovery-apply`. Le helper root classifie ensuite exactement la reprise
comme `fresh` ou `corrective`; aucun fallback décidé par l'interface n'est
admis. Le helper conserve sans
réécriture le marqueur, le WAL et l’audit `.10` sous le répertoire historique
root-only, publie un marqueur correctif one-shot, puis crée le successeur `.11`.
Une interruption rejoue uniquement cette même correction; elle ne réouvre ni
le bootstrap initial ni une deuxième tentative corrective.

Si le rollback avait déjà atteint sa phase terminale mais pas son dernier
nettoyage, le même parcours est explicitement sans mot de passe : le préflight
root atteste `rollback_finalize_resume`, puis Atlas autorise uniquement la
finalisation et n'envoie aucune trame de secrets.

## Sécurité des clés API

Chaque carte ou compte fournisseur ouvre un dialogue Atlas avec deux champs
masqués : le mot de passe OpenBao et la nouvelle clé API. Le backend n'accepte
qu'un identifiant de compte allowlisté et lance exclusivement :

```text
/usr/local/libexec/ops-model-key-manager set-stdin <provider_account_id>
```

Les deux valeurs passent par un pipe anonyme privé avec le framing binaire
`ATLASKEY1`; elles ne figurent jamais dans argv, l'environnement, stdout ou les
journaux. Le helper root-owned s'exécute sous l'identité `ops-user`, authentifie
une session humaine éphémère auprès d'OpenBao, réalise une écriture KV v2 avec
CAS puis révoque le token. Après la rotation confirmée, un second helper
root-owned sans argument republie le bundle local et recharge les consommateurs
fixes. Il active Hermes uniquement si une clé Qwen ou DeepSeek est publiée, et
le timer financier uniquement si une clé DeepSeek, Moonshot ou StepFun est
publiée. Aucun des deux helpers n'exécute de canari ni d'appel fournisseur.

## Dépendances Fedora et build

Selon les prérequis Linux officiels de Tauri v2, le poste de build doit disposer
de Rust et des bibliothèques WebKitGTK. Le graphe verrouillé courant exige Rust
1.88 ou plus récent. Sur Fedora :

```bash
sudo dnf install rust cargo rustfmt clippy gcc pkgconf-pkg-config rpm-build \
  webkit2gtk4.1-devel openssl-devel librsvg2-devel file
cargo install tauri-cli --version '=2.11.4' --locked
```

Node.js n'est pas requis pour ce frontend vanilla. Depuis `src-tauri` :

```bash
python3 ../../../scripts/validate-model-catalog.py \
  ../../../catalog/model-catalog.v2.json \
  --source ../../../catalog/sources/catalogue_modeles_IA_API_2026.txt
test -f Cargo.lock
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo tauri build --bundles rpm -- --locked
```

Le paquet Fedora est produit sous `target/release/bundle/rpm/`. Le build doit
utiliser le `Cargo.lock` versionné. Le paquet `.11` a été construit sous Fedora
44 avec `rustc`/`cargo` 1.98.0 et `tauri-cli` 2.11.4; son SHA-256 autoritatif
reste celui du manifeste, le lot n’étant pas déclaré reproductible octet pour
octet. L'installation du paquet et l'installation
des actifs orchestrateur/helper sont des changements opérationnels séparés ;
aucun des deux n'est exécuté automatiquement par le build.

Avant le gel d’une release, le bundle nommé
`target/release/bundle/rpm/Modeles IA-0.1.0-1.x86_64.rpm` doit être contrôlé
avec `rpm -qp`, puis copié vers le chemin exact consommé par le manifeste :
`/home/ops-user/Téléchargements/Atlas-modeles-IA-0.1.0-1.x86_64.rpm`. Un paquet et
un sidecar déjà présents sont d’abord déplacés vers des noms d’archive uniques;
ils ne sont jamais écrasés silencieusement. Le SHA-256 du fichier publié est
ensuite inscrit dans `artifacts.atlas_rpm.sha256`, son sidecar `.rpm.sha256` est
régénéré, et `sha256sum -c` ainsi que l’identité
`modeles-ia 0.1.0 1 x86_64` sont vérifiés avant le calcul de
`source.tree_sha256`.

Sur une machine où le RPM n'est pas encore installé, Atlas est amorcé une seule
fois par `scripts/enroll-atlas-trust-anchor`, puis toujours par le lanceur
root-owned versionné `launch-atlas-reviewed-rpm`. Ce lanceur sans privilège
vérifie le SHA-256
du RPM entier et son inventaire, extrait uniquement
`/usr/bin/ops-model-manager` dans un `memfd` anonyme, vérifie le SHA-256 du
payload, applique le mode `0500` et les quatre scellés Linux avant `execve`.
Il ne reçoit aucun argument ni mot de passe et ne crée aucune copie temporaire
du binaire. Après installation, le binaire `root:root` du RPM devient le chemin
normal de lancement.

Le dépôt contient actuellement l'application source, pas une preuve
d'installation. Atlas et le helper ne sont considérés déployés qu'après le
workflow d'exploitation autorisé, l'installation contrôlée des actifs et les
vérifications de [`../../docs/verification.md`](../../docs/verification.md).

Documentation Tauri de référence :

- <https://v2.tauri.app/start/create-project/> ;
- <https://v2.tauri.app/start/prerequisites/> ;
- <https://v2.tauri.app/security/permissions/> ;
- <https://v2.tauri.app/develop/calling-rust/>.

Le guide d'usage et les limites d'état sont détaillés dans
[`../../docs/model-manager.md`](../../docs/model-manager.md).
