"use strict";

// 登录失败时后端会 303 回 /login?error=invalid|locked，这里只做提示。
const error = new URLSearchParams(location.search).get("error");
const note = document.getElementById("error");
if (error === "invalid") {
  note.textContent = "用户名或密码不正确。";
  note.classList.add("show");
}
if (error === "locked") {
  note.textContent = "失败次数过多，请稍后再试。";
  note.classList.add("show");
}

const next = new URLSearchParams(location.search).get("next");
if (next && next.startsWith("/") && !next.startsWith("//")) {
  document.getElementById("next").value = next;
}
