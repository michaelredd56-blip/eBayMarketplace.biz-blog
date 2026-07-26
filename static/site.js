const toggle = document.querySelector('.menu-toggle');
const nav = document.querySelector('#primary-nav');
if (toggle && nav) {
  toggle.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(open));
  });
}

const passwordToggle = document.querySelector('[data-password-toggle]');
if (passwordToggle) {
  const passwordInput = document.getElementById(passwordToggle.getAttribute('aria-controls'));
  if (passwordInput) {
    passwordToggle.addEventListener('click', () => {
      const showing = passwordInput.type === 'text';
      passwordInput.type = showing ? 'password' : 'text';
      passwordToggle.textContent = showing ? 'Show' : 'Hide';
      passwordToggle.setAttribute('aria-pressed', String(!showing));
      passwordInput.focus();
    });
  }
}
