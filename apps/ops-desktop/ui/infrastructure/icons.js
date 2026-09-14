'use strict';
const opsIcons=new Set(["cpu", "memory-stick", "hard-drive", "network", "server", "monitor", "gamepad-2", "activity", "thermometer", "logs", "archive", "history", "clock", "refresh-cw", "sun-moon", "maximize-2", "minimize-2", "download", "radio", "database", "chevron-right", "arrow-up", "arrow-down", "list-filter", "layout-grid", "layers", "shield-check", "search", "chart-no-axes-combined", "check"]);
function opsIcon(name){return `<span class="ops-icon" data-icon="${opsIcons.has(name)?name:'activity'}" aria-hidden="true"></span>`;}
function hostIcon(id){return ({'dell-control':'monitor','edge-vps':'network',nas:'database',prod:'server',gamebox:'gamepad-2'})[id]||'server';}
function groupIcon(id){return ({cpu:'cpu',memory:'memory-stick',storage:'hard-drive',network:'network',sensors:'thermometer',system:'activity',all:'layout-grid'})[id]||'activity';}
