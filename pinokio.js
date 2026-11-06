module.exports = {
  version: "8.0.0",
  title: "Master AI Video Director Backend",
  description: "Local persistent video memory: 5 projects, 10 versions each; SQLite + SHA256 + offline reports",
  menu: async (kernel, info) => {
    let running = info.running("start.js")
    let local = info.local("start.js")
    return running ? [
      {default:true,text:"Local Backend",icon:"fa-solid fa-server",href:(local&&local.url)||"start.js"},
      {text:"Backend Logs",icon:"fa-solid fa-terminal",href:"start.js"}
    ] : [
      {default:true,text:"Start Backend",icon:"fa-solid fa-play",href:"start.js"}
    ]
  }
}