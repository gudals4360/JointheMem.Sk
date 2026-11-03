const form = document.querySelector("#verification-form");
const button = document.querySelector("#continue-button");
const names = { phone: "휴대폰 인증", certificate: "신한인증서", card: "카드 인증" };

form.addEventListener("change", (e) => {
  button.disabled = false;
  button.textContent = `${names[e.target.value]}으로 계속하기`;
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const picked = form.verification.value;
  if (!picked) return;
  // 실제 인증이 아닌 시연용 이동
  button.disabled = true;
  showToast(`${names[picked]} 선택 완료. 실제 인증 없이 다음 화면으로 이동해요.`);
  setTimeout(() => (location.href = "./signup-form.html"), 1500);
});
