/* Dependency-free checks for mount-picker state and the shipped JavaScript. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const appSource = fs.readFileSync(path.join(root, "app.py"), "utf8");
const html = appSource.slice(appSource.indexOf('HTML = r"""'));
const inline = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(inline);
new vm.Script(fs.readFileSync(path.join(root, "assets", "command-center.js"), "utf8"));
const css = fs.readFileSync(path.join(root, "assets", "command-center.css"), "utf8");
assert.match(css, /\.hidden\s*\{\s*display:\s*none\s*!important;\s*\}/);

let controls = [
  {className: "bks", dataset: {c: "example_app"}, checked: true},
  {className: "bkm", dataset: {c: "example_app", mkey: "bind:/upload"}, checked: true},
  {className: "bkm", dataset: {c: "example_app", mkey: "vol:nas"}, checked: false},
];
const context = vm.createContext({
  document: {
    querySelectorAll(selector) {
      if (selector === ".bks") return controls.filter(control => control.className === "bks");
      if (selector.startsWith('.bkm[data-c="')) {
        const name = selector.match(/data-c="([^"]+)"/)[1];
        return controls.filter(control => control.className === "bkm" && control.dataset.c === name);
      }
      return [];
    },
  },
  CSS: {escape: value => value},
  window: {addEventListener() {}},
  setInterval() {},
});
vm.runInContext(inline.replace(/\brefresh\(\);\s*$/, ""), context);
vm.runInContext(`MNT.example_app = [
  {kind:"bind",mkey:"bind:/upload",label:"library",dest:"/upload",network:false},
  {kind:"volume",mkey:"vol:nas",label:"NAS",dest:"/nas",network:true}
]; rememberBackupMounts();`, context);
const saved = JSON.parse(vm.runInContext("JSON.stringify(BK_MOUNTS.example_app)", context));
assert.deepEqual(saved, {all: false, mounts: ["bind:/upload"], skip_network: true});

controls = [];
const rebuilt = vm.runInContext('bkPanel("example_app")', context);
assert.match(rebuilt, /class="bks"[^>]*checked/);
assert.match(rebuilt, /data-mkey="bind:\/upload" checked/);
assert.doesNotMatch(rebuilt, /data-mkey="vol:nas" checked/);

vm.runInContext("BK_MOUNTS.example_app={all:false,mounts:[],skip_network:true}", context);
const none = vm.runInContext('bkPanel("example_app")', context);
assert.doesNotMatch(none, /class="bkm"[^>]*checked/);
vm.runInContext("BK_MOUNTS.example_app={all:true,mounts:[],skip_network:false}", context);
const all = vm.runInContext('bkPanel("example_app")', context);
assert.equal((all.match(/class="bkm"[^>]*checked/g) || []).length, 2);
console.log("PASS: inline and external JavaScript parse.");
console.log("PASS: mount subsets, None, All and network policy survive panel rebuilds.");
