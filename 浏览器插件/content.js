// 拾光 SeeGlow 内容脚本：B站视频页注入「拾光总结」按钮
// 点击 → 打开本机拾光（http://127.0.0.1:8765/?url=当前视频），自动开始总结
(function () {
  const APP = "http://127.0.0.1:8765/";

  function makeButton() {
    const btn = document.createElement("div");
    btn.id = "seeglow-btn";
    btn.textContent = "✦ 拾光总结";
    Object.assign(btn.style, {
      position: "fixed",
      right: "24px",
      bottom: "96px",
      zIndex: "99999",
      padding: "10px 18px",
      borderRadius: "99px",
      background: "linear-gradient(135deg,#f59e0b,#f97316)",
      color: "#fff",
      fontSize: "14px",
      fontWeight: "600",
      fontFamily: "PingFang SC, Microsoft YaHei, system-ui, sans-serif",
      cursor: "pointer",
      boxShadow: "0 6px 20px rgba(249,115,22,.4)",
      userSelect: "none",
      transition: "transform .1s, filter .15s",
    });
    btn.onmouseenter = () => (btn.style.filter = "brightness(1.08)");
    btn.onmouseleave = () => (btn.style.filter = "");
    btn.onmousedown = () => (btn.style.transform = "scale(.96)");
    btn.onmouseup = () => (btn.style.transform = "");
    btn.onclick = () => {
      window.open(APP + "?url=" + encodeURIComponent(location.href), "_blank");
    };
    return btn;
  }

  // B站是 SPA，随路由变化增删按钮
  const mo = new MutationObserver(() => {
    const onVideo = /\/video\/|\/bangumi\/play\//.test(location.pathname);
    const exists = document.getElementById("seeglow-btn");
    if (onVideo && !exists) document.body.appendChild(makeButton());
    if (!onVideo && exists) exists.remove();
  });
  mo.observe(document.body, { childList: true, subtree: true });
  if (/\/video\/|\/bangumi\/play\//.test(location.pathname)) {
    document.body.appendChild(makeButton());
  }
})();
