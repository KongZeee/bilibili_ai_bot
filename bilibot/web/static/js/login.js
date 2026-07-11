/* BiliBot 登录页脚本 */
document.getElementById('loginForm').onsubmit = async (e) => {
    e.preventDefault();
    const btn = document.getElementById('submitBtn');
    const error = document.getElementById('error');
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = '登录中…';
    error.style.display = 'none';
    try {
        const res = await fetch('/api/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                username: document.getElementById('username').value,
                password: document.getElementById('password').value
            })
        });
        const data = await res.json();
        if (data.success) {
            window.location.href = '/';
        } else {
            error.textContent = (data.error && data.error.message) || '登录失败';
            error.style.display = 'block';
        }
    } catch (err) {
        error.textContent = '网络错误';
        error.style.display = 'block';
    }
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    btn.textContent = '登录';
};
