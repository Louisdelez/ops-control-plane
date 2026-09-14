use std::{fs,path::Path};
const COMMANDS: &[&str]=&["get_native_budget","terminal_start","terminal_poll","terminal_write","terminal_resize","terminal_close","execution_status","execution_mode","open_service","reload_service","open_settings","settings_state","settings_enabled","settings_manage","settings_providers","provider_tabs","provider_browser","get_catalogue","get_runtime_snapshot","simulate_cost","preview_candidates","refresh_provider_finance","save_provider_credential","get_bootstrap_status","get_bootstrap_result"];
fn main() {
    let source=Path::new("../../model-manager/ui");
    let target=Path::new("../ui/atlas");
    fs::create_dir_all(target).unwrap();
    for name in ["index.html","styles.css","app.js","onboarding.js"] {
        println!("cargo:rerun-if-changed={}",source.join(name).display());
        let mut text=fs::read_to_string(source.join(name)).unwrap();
        if name=="index.html" {
            text=text.replace("<link rel=\"stylesheet\" href=\"./styles.css\">", "<link rel=\"stylesheet\" href=\"./styles.css\"><link rel=\"stylesheet\" href=\"./embedded.css\">");
            text=text.replace("    <script src=\"./onboarding.js\" defer></script>", "    <script src=\"./onboarding.js\" defer></script>\n    <script src=\"./embedded.js\" defer></script>");
            // The original install wizard stays in the source application. The
            // integrated host starts the catalogue on an already installed host.
            text=text.replace("<section aria-labelledby=\"accounts-title\">", "<aside class=\"notice notice--neutral\"><div><strong>Installation native · Ops</strong><p>Atlas utilise les services déjà installés. Les clés API peuvent être ajoutées à la fin.</p><details><summary>État historique de l’ancien installateur</summary><pre id=\"legacy-install-status\">Lecture…</pre></details><h3>Budgets du service natif</h3><p>Limites et consommation locales des fournisseurs API ; les soldes officiels restent distincts.</p><div id=\"native-budget-list\"></div></div></aside><section aria-labelledby=\"accounts-title\">");
        }
        if name=="app.js" {
            text=text.replace("La clé sera publiée aux services autorisés sans être renvoyée à Atlas.","La clé sera conservée dans OpenBao. Les services prévus activeront cet accès automatiquement, dans les budgets configurés.");
            text=text.replace("Après validation, les deux champs sont vidés puis supprimés du DOM. Aucun secret n’est journalisé ou placé dans le stockage du navigateur.","Après l’envoi, les champs sont effacés. Aucun mot de passe ni aucune clé n’est conservé dans l’application.");
            text=text.replace("    invalid_response: \"réponse runtime invalide\",", "    native_endpoint_unavailable: \"service natif connecté ; rapprochement des cartes, performances et soldes détaillés indisponibles\",\n    invalid_response: \"réponse runtime invalide\",");
            text=text.replace("Elle a été publiée uniquement aux services locaux autorisés. Atlas n’a reçu aucune copie en retour.", "Elle est enregistrée dans le coffre. Les services prévus prennent cet accès en compte automatiquement. La réussite des appels dépend encore de la validité de la clé et du compte fournisseur.");
        }
        fs::write(target.join(name),text).unwrap();
    }
    println!("cargo:rerun-if-changed=../integration/embedded.css");
    fs::copy("../integration/embedded.css",target.join("embedded.css")).unwrap();
    println!("cargo:rerun-if-changed=../integration/embedded.js");
    fs::copy("../integration/embedded.js",target.join("embedded.js")).unwrap();
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(COMMANDS))).unwrap();
}
