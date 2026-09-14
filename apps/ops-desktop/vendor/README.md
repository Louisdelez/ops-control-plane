# glib 0.18.5 — correctif compatible GTK3

La copie de glib conserve sa licence et son copyright d’origine. Le seul fichier Rust modifié est `src/variant_iter.rs` : `p` et la référence passée à la fonction C deviennent mutables. Il s’agit des deux lignes du [correctif officiel b5a4071e439bef2b5eea76c3aa25e5ae84839e34](https://github.com/gtk-rs/gtk-rs-core/commit/b5a4071e439bef2b5eea76c3aa25e5ae84839e34).

L’avis [RUSTSEC-2024-0429](https://rustsec.org/advisories/RUSTSEC-2024-0429.html) donne glib >=0.20 comme version publiée corrigée. GTK3 utilise encore glib 0.18 ; les deux racines Cargo Ops/Atlas utilisent donc cette copie via `[patch.crates-io]`. Aucun avis n’est ajouté à une liste d’exclusion et le numéro amont n’est pas falsifié. Un audit basé uniquement sur les versions ou les sources crates.io ne suffit pas pour cette dépendance locale : comparer les sources et exécuter la régression optimisée.

`apps/ops-desktop/src-tauri/tests/glib_variant_iterator.rs` vérifie les lectures avant/arrière, nth, nth_back et last. Avec le paquet crates.io original, ce test provoque un SIGSEGV en mode release sur ce poste. La preuve initiale et les empreintes sont dans `artifacts/continuation-2026-09-11/glib-before.log` et `glib-backport-provenance.json`.

Ce correctif ne résout pas l’absence de maintenance de GTK3 ni les autres avis de dépendances. Supprimer la copie seulement après adoption d’une pile amont compatible et corrigée, avec les mêmes tests.
