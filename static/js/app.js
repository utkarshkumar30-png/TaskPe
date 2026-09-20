// TaskPe - WhatsApp Task Earning Platform Client Logic

let currentUser = null;
let currentTasks = [];
let activeTask = null;
let countdownTimer = null;
let activeAdminTab = 'submissions';
let minimumWithdrawal = 0;
let workerUsdtInrRate = 0;
let selectedProofFiles = [];
let submissionProofUrls = {};
let activeLightboxProofs = [];
let passwordResetToken = null;
let passwordResetChallenge = null;

// ================= INITIALIZATION =================
document.addEventListener('DOMContentLoaded', async () => {
  await checkAuth();
  updateCalculator(10);
  document.getElementById('mobile-nav-home')?.classList.add('is-active');
});

// ================= NAVIGATION ROUTER =================
function navigate(viewName, params = {}) {
  // Views map
  const views = ['home', 'tasks', 'task-detail', 'wallet', 'admin', 'refer', 'leaderboard'];
  views.forEach(v => {
    const el = document.getElementById(`view-${v}`);
    if (el) el.classList.add('hidden');
  });

  // Highlight nav button
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.classList.remove('bg-slate-800', 'text-white', 'text-emerald-400');
    btn.classList.add('text-slate-300');
  });

  const activeNav = document.getElementById(`nav-${viewName}`);
  if (activeNav) {
    activeNav.classList.add('bg-slate-800', 'text-emerald-400');
    activeNav.classList.remove('text-slate-300');
  }

  const mobileView = viewName === 'task-detail' ? 'tasks' : viewName;
  document.querySelectorAll('.mobile-nav-btn').forEach(btn => btn.classList.remove('is-active'));
  const activeMobileNav = document.getElementById(`mobile-nav-${mobileView}`);
  if (activeMobileNav) activeMobileNav.classList.add('is-active');

  // Guard protected routes
  if (['wallet', 'refer', 'task-detail'].includes(viewName) && !currentUser) {
    showToast('Please log in to view this page.', 'info');
    openAuthModal('login');
    return;
  }

  if (viewName === 'admin') {
    if (!currentUser || currentUser.role !== 'admin') {
      showToast('Admin privilege required.', 'error');
      return;
    }
  }

  const targetEl = document.getElementById(`view-${viewName}`);
  if (targetEl) {
    targetEl.classList.remove('hidden');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // Trigger view data loaders
  if (viewName === 'tasks') loadTasks();
  if (viewName === 'wallet') loadWalletData();
  if (viewName === 'admin') loadAdminData();
  if (viewName === 'refer') loadReferralData();
  if (viewName === 'leaderboard') loadLeaderboard();
  if (viewName === 'task-detail' && params.taskId) loadTaskDetail(params.taskId);

  // Close dropdown if open
  closeUserDropdown();
}

// ================= AUTHENTICATION =================
async function checkAuth() {
  try {
    const res = await fetch('/api/auth/me');
    const data = await res.json();
    if (data.logged_in) {
      currentUser = data.user;
      renderUserLoggedIn();
    } else {
      currentUser = null;
      renderUserLoggedOut();
    }
  } catch (err) {
    console.error('Auth check error:', err);
    currentUser = null;
    renderUserLoggedOut();
  }
}

function renderUserLoggedIn() {
  // Login responses from older/staging databases may contain numeric values
  // serialized as strings. Normalize them before using number-only methods.
  currentUser.wallet_balance = Number(currentUser.wallet_balance) || 0;
  currentUser.total_earned = Number(currentUser.total_earned) || 0;
  currentUser.name = String(currentUser.name || 'User');
  currentUser.role = String(currentUser.role || 'user');
  document.getElementById('auth-buttons-logged-out').classList.add('hidden');
  document.getElementById('auth-buttons-logged-in').classList.remove('hidden');
  document.getElementById('earner-stats-strip').classList.remove('hidden');

  // Update navbar wallet & user info
  document.getElementById('nav-wallet-balance').innerText = currentUser.wallet_balance.toFixed(2);
  document.getElementById('nav-user-name').innerText = currentUser.name.split(' ')[0];
  document.getElementById('nav-user-initial').innerText = currentUser.name.charAt(0).toUpperCase();
  document.getElementById('dropdown-user-email').innerText = currentUser.email;
  document.getElementById('dropdown-user-role').innerText = currentUser.role.toUpperCase();

  // Admin button in navbar
  const adminNav = document.getElementById('nav-admin');
  if (currentUser.role === 'admin') {
    adminNav.classList.remove('hidden');
  } else {
    adminNav.classList.add('hidden');
  }

  // Update earner dashboard stats
  document.getElementById('stat-wallet-bal').innerText = currentUser.wallet_balance.toFixed(2);
  document.getElementById('stat-total-earned').innerText = currentUser.total_earned.toFixed(2);
}

function renderUserLoggedOut() {
  document.getElementById('auth-buttons-logged-out').classList.remove('hidden');
  document.getElementById('auth-buttons-logged-in').classList.add('hidden');
  document.getElementById('nav-admin').classList.add('hidden');
  document.getElementById('earner-stats-strip').classList.add('hidden');
}

async function handleLoginSubmit(e) {
  e.preventDefault();
  const identifier = document.getElementById('login-identifier').value.trim();
  const password = document.getElementById('login-password').value;

  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identifier, password })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      currentUser = data.user;
      renderUserLoggedIn();
      closeAuthModal();
      navigate('tasks');
    } else {
      showToast(data.message, 'error');
    }
  } catch (err) {
    showToast('Failed to log in. Please retry.', 'error');
  }
}

function togglePasswordVisibility(inputId, button) {
  const input = document.getElementById(inputId);
  const visible = input.type === 'text';
  input.type = visible ? 'password' : 'text';
  button.setAttribute('aria-label', visible ? 'Show password' : 'Hide password');
  const icon = button.querySelector('i');
  if (icon) icon.className = visible ? 'fa-solid fa-eye' : 'fa-solid fa-eye-slash';
}

function openForgotPassword() {
  passwordResetToken = null;
  passwordResetChallenge = null;
  resetForgotPasswordForm();
  document.getElementById('auth-tab-login').parentElement.classList.add('hidden');
  document.getElementById('form-login').classList.add('hidden');
  document.getElementById('form-signup').classList.add('hidden');
  document.getElementById('form-reset-password').classList.add('hidden');
  document.getElementById('form-forgot-password').classList.remove('hidden');
}

async function handleForgotPasswordSubmit(e) {
  e.preventDefault();
  const mobile = document.getElementById('reset-mobile').value.trim();
  try {
    const res = await fetch('/api/auth/forgot-password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mobile }) });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.message || 'Unable to send OTP.');
    passwordResetChallenge = data.challenge_token || null;
    document.getElementById('form-forgot-password').classList.add('hidden');
    document.getElementById('form-reset-password').classList.remove('hidden');
    showToast(data.message, 'success');
  } catch (err) { showToast(err.message || 'Unable to send OTP. Please retry.', 'error'); }
}

async function handleResetPasswordSubmit(e) {
  e.preventDefault();
  const mobile = document.getElementById('reset-mobile').value.trim();
  const otp_code = document.getElementById('reset-otp').value.trim();
  try {
    if (!passwordResetToken) {
      const verifyRes = await fetch('/api/auth/forgot-password/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mobile, challenge_token: passwordResetChallenge, otp_code }) });
      const verifyData = await verifyRes.json();
      if (!verifyRes.ok || !verifyData.success) throw new Error(verifyData.message || 'Invalid or expired verification code.');
      passwordResetToken = verifyData.reset_token;
      document.getElementById('reset-password-fields').classList.remove('hidden');
      document.getElementById('reset-new-password').required = true;
      document.getElementById('reset-confirm-password').required = true;
      document.getElementById('reset-form-title').textContent = 'Create a new password';
      document.getElementById('reset-form-help').textContent = 'Choose a new password for your TaskPe account.';
      document.getElementById('reset-submit-button').textContent = 'Reset Password';
      showToast('OTP verified. Create a new password.', 'success');
      return;
    }
    const new_password = document.getElementById('reset-new-password').value;
    const confirm_password = document.getElementById('reset-confirm-password').value;
    const resetRes = await fetch('/api/auth/reset-password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reset_token: passwordResetToken, new_password, confirm_password }) });
    const resetData = await resetRes.json();
    if (!resetRes.ok || !resetData.success) throw new Error(resetData.message || 'Unable to reset password.');
    passwordResetToken = null;
    passwordResetChallenge = null;
    showToast(resetData.message, 'success');
    switchAuthTab('login');
  } catch (err) { showToast(err.message || 'Password reset failed. Please retry.', 'error'); }
}

function resetForgotPasswordForm() {
  const setValue = (id, value) => { const element = document.getElementById(id); if (element) element.value = value; };
  const setClass = (id, className) => { const element = document.getElementById(id); if (element) element.className = className; };
  const setText = (id, value) => { const element = document.getElementById(id); if (element) element.textContent = value; };
  setValue('reset-mobile', '');
  setValue('reset-otp', '');
  setValue('reset-new-password', '');
  setValue('reset-confirm-password', '');
  const fields = document.getElementById('reset-password-fields');
  if (fields) fields.classList.add('hidden');
  const newPassword = document.getElementById('reset-new-password');
  const confirmPassword = document.getElementById('reset-confirm-password');
  if (newPassword) newPassword.required = false;
  if (confirmPassword) confirmPassword.required = false;
  setText('reset-form-title', 'Verify OTP');
  setText('reset-form-help', 'Enter the OTP sent to your mobile.');
  setText('reset-submit-button', 'Verify OTP');
}

async function handleSignupSubmit(e) {
  e.preventDefault();
  const name = document.getElementById('signup-name').value.trim();
  const mobile = document.getElementById('signup-mobile').value.trim();
  const email = document.getElementById('signup-email').value.trim();
  const password = document.getElementById('signup-password').value;
  const referral_code = document.getElementById('signup-referral').value.trim();
  const otp_code = document.getElementById('signup-otp').value.trim();

  if (document.getElementById('signup-otp-container').classList.contains('hidden')) {
    // Prompt OTP first
    sendMobileOtp();
    return;
  }

  // Verify OTP first
  try {
    const otpRes = await fetch('/api/auth/verify-otp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mobile, otp_code })
    });
    const otpData = await otpRes.json();
    if (!otpData.success) {
      showToast(otpData.message || 'Invalid OTP', 'error');
      return;
    }
  } catch (err) {
    showToast('OTP verification failed', 'error');
    return;
  }

  // Register
  try {
    const res = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, mobile, email, password, referral_code })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      currentUser = data.user;
      renderUserLoggedIn();
      closeAuthModal();
      navigate('tasks');
    } else {
      showToast(data.message, 'error');
    }
  } catch (err) {
    showToast('Sign up failed. Please try again.', 'error');
  }
}

function switchSignupMethod(method) {
  const mobileForm = document.getElementById('form-signup');
  const emailForm = document.getElementById('form-signup-email');
  if (!mobileForm || !emailForm) return;
  mobileForm.classList.toggle('hidden', method !== 'mobile');
  emailForm.classList.toggle('hidden', method !== 'email');
}

async function sendEmailVerification() {
  const email = document.getElementById('email-signup-email').value.trim();
  try {
    const response = await fetch('/api/auth/send-email-verification', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email })
    });
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.message || 'Unable to send verification.');
    if (data.verification_token) document.getElementById('email-signup-token').value = data.verification_token;
    showToast(data.message, 'success');
  } catch (err) { showToast(err.message || 'Unable to send verification.', 'error'); }
}

async function handleEmailSignupSubmit(e) {
  e.preventDefault();
  const payload = {
    name: document.getElementById('email-signup-name').value.trim(),
    email: document.getElementById('email-signup-email').value.trim(),
    password: document.getElementById('email-signup-password').value,
    referral_code: document.getElementById('email-signup-referral').value.trim(),
    verification_token: document.getElementById('email-signup-token').value.trim()
  };
  try {
    const response = await fetch('/api/auth/verify-email', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: payload.email, verification_token: payload.verification_token }) });
    const verification = await response.json();
    if (!response.ok || !verification.success) throw new Error(verification.message || 'Verify your email before registering.');
    const registerResponse = await fetch('/api/auth/register-email', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const data = await registerResponse.json();
    if (!registerResponse.ok || !data.success) throw new Error(data.message || 'Email signup failed.');
    showToast(data.message, 'success');
    currentUser = data.user;
    renderUserLoggedIn();
    closeAuthModal();
    navigate('tasks');
  } catch (err) { showToast(err.message || 'Email signup failed.', 'error'); }
}

async function sendMobileOtp() {
  const mobile = document.getElementById('signup-mobile').value.trim();
  if (!mobile || mobile.length < 10) {
    showToast('Please enter a 10-digit mobile number first.', 'error');
    return;
  }

  try {
    const res = await fetch('/api/auth/send-otp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mobile })
    });
    const data = await res.json();
    if (data.success) {
      document.getElementById('signup-otp-container').classList.remove('hidden');
      document.getElementById('signup-otp').value = '';
      showToast('OTP sent. Enter the code you received.', 'success');
    } else {
      showToast(data.message, 'error');
    }
  } catch (err) {
    showToast('Failed to send OTP.', 'error');
  }
}

async function logout(toGuest = false) {
  try {
    const logoutResponse = await fetch('/api/auth/logout', { method: 'POST' });
    if (!logoutResponse.ok) throw new Error('Logout failed.');
    // logout() clears the Flask session, including its CSRF token. Refresh the
    // page-level token before any subsequent login/signup/reset POST.
    await refreshCsrfToken();
    currentUser = null;
    renderUserLoggedOut();
    showToast(toGuest ? 'Switched to Guest mode.' : 'Logged out.', 'info');
    navigate('home');
  } catch (err) {
    console.error(err);
  }
}

async function refreshCsrfToken() {
  const response = await fetch('/api/auth/csrf', { cache: 'no-store' });
  const data = await response.json();
  if (!response.ok || !data.success || !data.csrf_token) throw new Error('Unable to refresh CSRF token.');
  window.__TASKPE_CSRF__ = data.csrf_token;
  return data.csrf_token;
}

function openAuthModal(tab = 'login') {
  document.getElementById('modal-auth').classList.remove('hidden');
  document.body.classList.add('modal-open');
  switchAuthTab(tab);
}

function closeAuthModal() {
  passwordResetToken = null;
  passwordResetChallenge = null;
  resetForgotPasswordForm();
  document.getElementById('modal-auth').classList.add('hidden');
  document.body.classList.remove('modal-open');
}

function openMobileProfile() {
  if (!currentUser) {
    openAuthModal('login');
    return;
  }
  toggleUserDropdown();
}

function switchAuthTab(tab) {
  const loginTab = document.getElementById('auth-tab-login');
  const signupTab = document.getElementById('auth-tab-signup');
  const loginForm = document.getElementById('form-login');
  const signupForm = document.getElementById('form-signup');
  const emailSignupForm = document.getElementById('form-signup-email');
  const forgotForm = document.getElementById('form-forgot-password');
  const resetForm = document.getElementById('form-reset-password');
  const tabs = loginTab.parentElement;
  tabs.classList.remove('hidden');
  forgotForm.classList.add('hidden');
  resetForm.classList.add('hidden');
  emailSignupForm.classList.add('hidden');

  if (tab === 'login') {
    loginTab.classList.add('text-emerald-400', 'border-emerald-400');
    loginTab.classList.remove('text-slate-400', 'border-transparent');
    signupTab.classList.add('text-slate-400', 'border-transparent');
    signupTab.classList.remove('text-emerald-400', 'border-emerald-400');
    loginForm.classList.remove('hidden');
    signupForm.classList.add('hidden');
    emailSignupForm.classList.add('hidden');
  } else {
    signupTab.classList.add('text-emerald-400', 'border-emerald-400');
    signupTab.classList.remove('text-slate-400', 'border-transparent');
    loginTab.classList.add('text-slate-400', 'border-transparent');
    loginTab.classList.remove('text-emerald-400', 'border-emerald-400');
    signupForm.classList.remove('hidden');
    switchSignupMethod('mobile');
    loginForm.classList.add('hidden');
  }
}

function toggleUserDropdown() {
  document.getElementById('user-dropdown-menu').classList.toggle('hidden');
}

function closeUserDropdown() {
  const menu = document.getElementById('user-dropdown-menu');
  if (menu) menu.classList.add('hidden');
}

// ================= TASK CATALOG =================
async function loadTasks() {
  try {
    const res = await fetch('/api/tasks');
    const data = await res.json();
    if (data.success) {
      currentTasks = data.tasks;
      renderTasksGrid(currentTasks);
      document.getElementById('tasks-count-badge').innerText = `${currentTasks.length} Tasks`;

      // Update earner stats if logged in
      if (currentUser) {
        const completedCount = currentTasks.filter(t => t.user_submission_status === 'approved').length;
        const pendingTasks = currentTasks.filter(t => t.user_submission_status === 'pending');
        const pendingSum = pendingTasks.reduce((sum, t) => sum + t.payout, 0);

        document.getElementById('stat-tasks-done').innerText = completedCount;
        document.getElementById('stat-pending-amt').innerText = pendingSum.toFixed(2);
      }
    }
  } catch (err) {
    showToast('Failed to load tasks', 'error');
  }
}

function filterTasks(cat) {
  // Update filter buttons UI
  document.querySelectorAll('.filter-btn').forEach(b => {
    b.classList.remove('bg-emerald-500', 'text-slate-950');
    b.classList.add('bg-slate-800', 'text-slate-300');
    if (b.dataset.cat === cat) {
      b.classList.remove('bg-slate-800', 'text-slate-300');
      b.classList.add('bg-emerald-500', 'text-slate-950');
    }
  });

  if (cat === 'all') {
    renderTasksGrid(currentTasks);
  } else {
    const filtered = currentTasks.filter(t => t.category === cat);
    renderTasksGrid(filtered);
  }
}

function renderTasksGrid(tasks) {
  const grid = document.getElementById('tasks-grid');
  if (!grid) return;

  if (tasks.length === 0) {
    grid.innerHTML = `
      <div class="col-span-full text-center py-12 text-slate-400 glass-card rounded-2xl">
        <i class="fa-solid fa-list-check text-3xl mb-2 text-slate-500"></i>
        <p>No tasks found in this category.</p>
      </div>
    `;
    return;
  }

  grid.innerHTML = tasks.map(task => {
    const taskId = Number(task.id);
    const category = escapeHtml(task.category);
    const title = escapeHtml(task.title);
    const description = escapeHtml(task.description);
    const timeLimit = escapeHtml(task.time_limit_minutes);
    const targetCount = escapeHtml(task.target_count);
    const availableSlots = escapeHtml(task.available_slots ?? task.slots_remaining);
    const totalSlots = escapeHtml(task.total_slots);
    const takenSlots = escapeHtml(task.taken_slots ?? (task.total_slots - task.slots_remaining));
    const completedSlots = escapeHtml(task.completed_slots);
    const payout = Number(task.payout);
    const perMessageRate = Number(task.per_message_rate);
    const payoutText = Number.isFinite(payout) ? payout.toFixed(2) : '0.00';
    const perMessageRateText = Number.isFinite(perMessageRate) ? perMessageRate.toFixed(2) : '0.00';
    let actionBtnHtml = '';
    if (task.campaign_active && task.active_task_id === task.id) {
      actionBtnHtml = `
        <button onclick="navigate('task-detail', {taskId: ${taskId}})" class="w-full py-2.5 rounded-xl bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 text-xs font-bold transition flex items-center justify-center gap-1.5">
          <i class="fa-solid fa-hourglass-half animate-pulse"></i> ${task.campaign_active_status === 'pending_review' ? 'In Review (Click to view)' : 'Continue Task'}
        </button>
      `;
    } else if (task.campaign_active) {
      actionBtnHtml = `
        <button disabled class="w-full py-2.5 rounded-xl bg-slate-800 text-slate-400 border border-slate-700 text-xs font-bold cursor-not-allowed flex items-center justify-center gap-1.5">
          <i class="fa-solid fa-lock"></i> ${category} Task Already Active
        </button>
      `;
    } else {
      actionBtnHtml = `
        <button onclick="startTask(${taskId})" class="w-full py-2.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs shadow-md glow-wa-sm transition flex items-center justify-center gap-2">
          <span>Start Task</span>
          <i class="fa-solid fa-arrow-right text-[11px]"></i>
        </button>
      `;
    }

    const pct = Math.min(100, Math.round((task.completed_slots / task.total_slots) * 100));

    return `
      <div class="glass-card rounded-2xl p-5 flex flex-col justify-between hover:border-emerald-500/40 transition">
        <div>
          <!-- Category & Time -->
          <div class="flex items-center justify-between gap-2 mb-3">
            <span class="px-2.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-emerald-500/15 text-emerald-400 border border-emerald-500/20">
              ${category}
            </span>
            <span class="text-xs text-slate-400 flex items-center gap-1">
              <i class="fa-regular fa-clock text-[11px]"></i> ${timeLimit} Mins
            </span>
          </div>

          <!-- Title -->
          <h3 class="font-bold text-white text-base leading-snug mb-2">${title}</h3>
          <p class="text-xs text-slate-300 line-clamp-2 mb-4">${description}</p>

          <!-- Numbers count pill -->
          <div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-800 text-[11px] text-slate-300 mb-4">
            <i class="fa-brands fa-whatsapp text-emerald-400"></i>
            <span>${targetCount} WhatsApp Numbers</span>
          </div>

          <!-- Progress / Slots remaining -->
          <div class="mb-4">
            <div class="flex justify-between text-[11px] text-slate-400 mb-1">
              <span>Slots</span>
              <span>Available: ${availableSlots} / ${totalSlots}</span>
            </div>
            <div class="flex justify-between text-[11px] text-slate-500 mb-2"><span>Taken: ${takenSlots}</span><span>Completed: ${completedSlots}</span></div>
            <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
              <div class="bg-emerald-500 h-1.5 rounded-full" style="width: ${pct}%"></div>
            </div>
          </div>
        </div>

        <!-- Footer / Payout & CTA -->
        <div class="pt-4 border-t border-slate-800/80">
          <div class="flex items-baseline justify-between mb-3">
            <span class="text-xs text-slate-400">Pay: ₹${payoutText} for ${targetCount} numbers<br>(₹${perMessageRateText} per message)</span>
            <span class="text-xl font-extrabold text-emerald-400 font-heading">₹${payoutText}</span>
          </div>
          ${actionBtnHtml}
        </div>
      </div>
    `;
  }).join('');
}

async function startTask(taskId) {
  try {
    const res = await fetch(`/api/tasks/${taskId}/take`, { method: 'POST' });
    const data = await res.json();
    if (!data.success) {
      showToast(data.message || 'Unable to start this task', 'error');
      loadTasks();
      return;
    }
    showToast(data.message, 'success');
    navigate('task-detail', { taskId });
  } catch (err) {
    showToast('Unable to start this task', 'error');
  }
}

// ================= TASK DETAIL & EXECUTION =================
async function loadTaskDetail(taskId) {
  try {
    const res = await fetch(`/api/tasks/${taskId}`);
    const data = await res.json();
    if (!data.success) {
      showToast(data.message || 'Task not found', 'error');
      navigate('tasks');
      return;
    }

    activeTask = data.task;
    renderTaskDetailView(activeTask);
    await loadServerCountdown(activeTask.id);
  } catch (err) {
    showToast('Failed to load task details', 'error');
  }
}

function renderTaskDetailView(task) {
  document.getElementById('detail-task-title').innerText = task.title;
  document.getElementById('detail-task-category').innerText = task.category;
  document.getElementById('detail-task-time').innerText = task.time_limit_minutes;
  document.getElementById('detail-task-desc').innerText = task.description;
  document.getElementById('detail-task-payout').innerText = task.payout.toFixed(2);
  document.getElementById('submit-btn-payout').innerText = task.payout.toFixed(2);
  const preparedMessage = task.destination_link && !task.template_text.includes(task.destination_link)
    ? `${task.template_text}\n\n${task.destination_link}` : task.template_text;
  document.getElementById('detail-template-text').innerText = preparedMessage;
  document.getElementById('detail-proof-instructions').innerText = task.proof_instructions;
  document.getElementById('detail-numbers-count').innerText = `(${task.target_numbers.length} Numbers)`;
  const asset = document.getElementById('detail-campaign-asset');
  const imageView = document.getElementById('detail-image-view');
  const imageDownload = document.getElementById('detail-image-download');
  const workflow = document.getElementById('detail-image-workflow');
  const image = document.getElementById('detail-task-image');
  const link = document.getElementById('detail-destination-link');
  const imageTask = task.task_type === 'IMAGE_TEXT_LINK';
  asset.classList.toggle('hidden', !imageTask);
  workflow.classList.toggle('hidden', !imageTask);
  imageView.classList.toggle('hidden', !imageTask || !task.image_url);
  imageDownload.classList.toggle('hidden', !imageTask || !task.image_url);
  imageDownload.classList.toggle('inline-flex', imageTask && !!task.image_url);
  image.classList.toggle('hidden', !imageTask || !task.image_url);
  if (task.image_url) { image.src = task.image_url; imageView.href = task.image_url; imageDownload.href = task.image_url; }
  link.classList.toggle('hidden', !imageTask || !task.destination_link);
  if (task.destination_link) { link.href = task.destination_link; link.textContent = task.destination_link; }

  // Render Target Numbers with WhatsApp Direct links (wa.me)
  const numbersList = document.getElementById('detail-numbers-list');
  const openedRecipients = new Set(JSON.parse(localStorage.getItem(`taskpe-opened-${task.id}`) || '[]'));

  numbersList.innerHTML = task.target_numbers.map((num, i) => {
    const cleanNum = String(num ?? '').replace(/\D/g, '');
    const waUrl = buildWhatsAppUrl(cleanNum, preparedMessage);
    if (!waUrl) return '';
    const opened = openedRecipients.has(cleanNum);

    return `
      <div class="flex items-center justify-between p-2.5 rounded-xl bg-slate-950 border border-slate-800/80 hover:border-emerald-500/30 transition">
        <div class="flex items-center gap-2">
          <span class="w-5 h-5 rounded-full bg-slate-800 text-slate-400 text-[10px] font-bold flex items-center justify-center">${i + 1}</span>
          <span class="text-xs font-mono font-semibold text-white">+${cleanNum}</span>
        </div>
        <div class="flex items-center gap-1.5">
          <button type="button" onclick="copyText('+${cleanNum}', 'Number copied!')" class="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition text-[11px]" title="Copy Number">
            <i class="fa-regular fa-copy"></i>
          </button>
          <span id="recipient-state-${cleanNum}" class="text-[10px] ${opened ? 'text-emerald-400' : 'text-slate-500'}">${opened ? 'Opened' : 'Pending'}</span>
          <a href="${waUrl}" target="_blank" rel="noopener noreferrer" onclick="markRecipientOpened(${task.id}, '${cleanNum}')" class="min-h-11 px-3 py-1 rounded-lg bg-emerald-500/20 hover:bg-emerald-500 text-emerald-300 hover:text-slate-950 text-xs font-bold transition flex items-center gap-1">
            <i class="fa-brands fa-whatsapp text-sm"></i>
            <span>Open WhatsApp</span>
          </a>
        </div>
      </div>
    `;
  }).join('');

  // Handle existing submission banner
  const subBanner = document.getElementById('existing-submission-banner');
  const subForm = document.getElementById('proof-submission-form');
  const cancelButton = document.getElementById('cancel-task-btn');
  const assignmentIsActive = task.assignment_status === 'in_progress';
  cancelButton.classList.toggle('hidden', !assignmentIsActive);

  if (task.user_submission) {
    subBanner.classList.remove('hidden');
    if (task.user_submission.status === 'approved') {
      subBanner.className = 'mb-4 p-4 rounded-2xl border border-emerald-500/40 bg-emerald-950/20 text-emerald-300 text-xs';
      subBanner.innerHTML = `
        <div class="flex items-center gap-2 font-bold mb-1">
          <i class="fa-solid fa-circle-check text-base"></i> Task Approved & Credited!
        </div>
        <p>Your screenshot proof was approved. ₹${task.payout.toFixed(2)} has been added to your wallet balance.</p>
      `;
      subForm.classList.add('hidden');
    } else if (task.user_submission.status === 'pending') {
      subBanner.className = 'mb-4 p-4 rounded-2xl border border-amber-500/40 bg-amber-950/20 text-amber-300 text-xs';
      subBanner.innerHTML = `
        <div class="flex items-center gap-2 font-bold mb-1">
          <i class="fa-solid fa-hourglass-half text-base animate-pulse"></i> Proof Under Review
        </div>
        <p>You have submitted proof for this task on ${escapeHtml(task.user_submission.submitted_at)}. Admin is reviewing it (usually takes 15–30 mins).</p>
      `;
      subForm.classList.add('hidden');
    } else if (task.user_submission.status === 'rejected') {
      subBanner.className = 'mb-4 p-4 rounded-2xl border border-red-500/40 bg-red-950/20 text-red-300 text-xs';
      subBanner.innerHTML = `
        <div class="flex items-center gap-2 font-bold mb-1">
          <i class="fa-solid fa-circle-xmark text-base"></i> Previous Proof Rejected
        </div>
        <p>Reason: "${escapeHtml(task.user_submission.admin_feedback || 'Unclear screenshot')}". You may re-upload a clear screenshot below.</p>
      `;
      subForm.classList.remove('hidden');
    }
  } else if (!assignmentIsActive) {
    subBanner.className = 'mb-4 p-4 rounded-2xl border border-amber-500/40 bg-amber-950/20 text-amber-300 text-xs';
    subBanner.classList.remove('hidden');
    subBanner.innerHTML = '<div class="font-bold mb-1"><i class="fa-solid fa-clock"></i> Task time expired or is not active</div><p>This slot has been released. Return to the dashboard to start another available task.</p>';
    subForm.classList.add('hidden');
  } else {
    subBanner.classList.add('hidden');
    subForm.classList.remove('hidden');
  }

  // Reset file dropzone
  selectedProofFiles = [];
  document.getElementById('proof-file-input').value = '';
  renderProofPreviews();
}

function buildWhatsAppUrl(phone, message) {
  const normalizedPhone = String(phone ?? '').replace(/\D/g, '');
  if (!normalizedPhone) return null;
  return `https://wa.me/${normalizedPhone}?text=${encodeURIComponent(String(message ?? ''))}`;
}

function markRecipientOpened(taskId, number) {
  const key = `taskpe-opened-${taskId}`;
  const opened = new Set(JSON.parse(localStorage.getItem(key) || '[]'));
  opened.add(number);
  localStorage.setItem(key, JSON.stringify([...opened]));
  const state = document.getElementById(`recipient-state-${number}`);
  if (state) { state.textContent = 'Opened'; state.className = 'text-[10px] text-emerald-400'; }
}

async function loadServerCountdown(taskId) {
  try {
    const res = await fetch(`/api/get_task_time/${taskId}`);
    const data = await res.json();
    if (!data.success || data.status !== 'in_progress') {
      startCountdown(0, taskId);
      return;
    }
    startCountdown(data.remaining_seconds, taskId);
  } catch (err) {
    showToast('Unable to verify the task timer. Please refresh.', 'error');
  }
}

function startCountdown(seconds, taskId) {
  if (countdownTimer) clearInterval(countdownTimer);
  let timeLeft = seconds;
  const display = document.getElementById('task-timer-display');
  let lastServerSync = Date.now();

  const update = async () => {
    const mins = Math.floor(timeLeft / 60);
    const secs = timeLeft % 60;
    display.innerText = `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    if (timeLeft <= 0) {
      clearInterval(countdownTimer);
      display.innerText = "00:00 (Expired)";
      document.getElementById('cancel-task-btn').classList.add('hidden');
      if (taskId) {
        await fetch(`/api/get_task_time/${taskId}`);
        showToast('Task time expired. Your slot has been released.', 'info');
        loadTasks();
      }
      return;
    }
    timeLeft--;
    // Re-sync frequently enough that system clock changes cannot extend a task.
    if (taskId && Date.now() - lastServerSync >= 30000) {
      lastServerSync = Date.now();
      try {
        const res = await fetch(`/api/get_task_time/${taskId}`);
        const data = await res.json();
        if (!data.success || data.status !== 'in_progress') {
          timeLeft = 0;
        } else {
          timeLeft = data.remaining_seconds;
        }
      } catch (_) { /* keep showing the last server-confirmed value */ }
    }
  };

  update();
  countdownTimer = setInterval(update, 1000);
}

async function cancelActiveTask() {
  if (!activeTask || !confirm('Cancel this task and release the slot? You cannot submit proof after cancelling.')) return;
  const button = document.getElementById('cancel-task-btn');
  button.disabled = true;
  try {
    const res = await fetch(`/api/tasks/${activeTask.id}/cancel`, { method: 'POST' });
    const data = await res.json();
    if (!data.success) throw new Error(data.message);
    if (countdownTimer) clearInterval(countdownTimer);
    showToast(data.message, 'success');
    await loadTasks();
    navigate('tasks');
  } catch (err) {
    showToast(err.message || 'Unable to cancel task', 'error');
  } finally {
    button.disabled = false;
  }
}

function copyTemplateText() {
  if (!activeTask) return;
  const message = activeTask.destination_link && !activeTask.template_text.includes(activeTask.destination_link)
    ? `${activeTask.template_text}\n\n${activeTask.destination_link}` : activeTask.template_text;
  navigator.clipboard.writeText(message).then(() => {
    const label = document.getElementById('copy-btn-label');
    label.innerText = 'Copied!';
    showToast('Template copied to clipboard!', 'success');
    setTimeout(() => { label.innerText = 'Copy Template'; }, 2000);
  });
}

function copyText(str, msg = 'Copied!') {
  navigator.clipboard.writeText(str).then(() => {
    showToast(msg, 'success');
  });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function copyEncodedText(encoded, msg) {
  copyText(decodeURIComponent(encoded), msg);
}

function previewProofImage(input) {
  const incoming = Array.from(input.files || []);
  if (!incoming.length) return;
  const remaining = 8 - selectedProofFiles.length;
  if (incoming.length > remaining) {
    showToast(`You can upload a maximum of 8 screenshots. ${remaining} more can be added.`, 'error');
  }
  selectedProofFiles.push(...incoming.slice(0, Math.max(0, remaining)));
  input.value = '';
  renderProofPreviews();
}

function renderProofPreviews() {
  const list = document.getElementById('proof-preview-list');
  const empty = document.getElementById('dropzone-empty-state');
  const preview = document.getElementById('dropzone-preview-state');
  const count = document.getElementById('proof-image-count');
  if (!list || !empty || !preview || !count) return;
  empty.classList.toggle('hidden', selectedProofFiles.length > 0);
  preview.classList.toggle('hidden', selectedProofFiles.length === 0);
  count.innerHTML = `<i class="fa-solid fa-circle-check"></i> ${selectedProofFiles.length} of 8 screenshots selected`;
  list.replaceChildren();
  selectedProofFiles.forEach((file, index) => {
    const tile = document.createElement('div');
    tile.className = 'relative aspect-square rounded-lg overflow-hidden border border-slate-700 bg-slate-950';
    const img = document.createElement('img');
    img.src = URL.createObjectURL(file);
    img.alt = `Screenshot ${index + 1}`;
    img.className = 'w-full h-full object-cover';
    img.onload = () => URL.revokeObjectURL(img.src);
    const badge = document.createElement('span');
    badge.className = 'absolute top-1 left-1 px-1.5 py-0.5 rounded bg-slate-950/90 text-[10px] font-bold text-white';
    badge.textContent = index + 1;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'absolute top-1 right-1 w-6 h-6 rounded-full bg-red-500 text-white text-xs';
    remove.setAttribute('aria-label', `Remove screenshot ${index + 1}`);
    remove.innerHTML = '<i class="fa-solid fa-xmark"></i>';
    remove.onclick = (event) => { event.stopPropagation(); removeProofImage(index); };
    tile.append(img, badge, remove);
    list.appendChild(tile);
  });
}

function removeProofImage(index) {
  if (Number.isInteger(index)) selectedProofFiles.splice(index, 1);
  else selectedProofFiles = [];
  renderProofPreviews();
}

async function submitProofForm(e) {
  e.preventDefault();
  if (!activeTask) return;

  if (!selectedProofFiles.length) {
    showToast('Please upload at least one screenshot image proof.', 'error');
    return;
  }

  const formData = new FormData();
  selectedProofFiles.forEach(file => formData.append('proof_images', file));
  formData.append('notes', document.getElementById('proof-notes').value.trim());

  const submitBtn = document.getElementById('submit-proof-btn');
  const originalHtml = submitBtn.innerHTML;
  submitBtn.disabled = true;
  submitBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Uploading Proof...`;

  try {
    const res = await fetch(`/api/tasks/${activeTask.id}/submit`, {
      method: 'POST',
      body: formData
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      if (countdownTimer) clearInterval(countdownTimer);
      await checkAuth();
      navigate('tasks');
    } else {
      showToast(data.message || 'Submission failed', 'error');
    }
  } catch (err) {
    showToast('Upload error. Please retry.', 'error');
  } finally {
    submitBtn.disabled = false;
    submitBtn.innerHTML = originalHtml;
  }
}

// ================= WALLET & WITHDRAWALS =================
async function loadWalletData() {
  try {
    const res = await fetch('/api/wallet/summary');
    const data = await res.json();
    if (data.success) {
      document.getElementById('wallet-card-balance').innerText = data.wallet.balance.toFixed(2);
      document.getElementById('wallet-breakdown-earned').innerText = data.wallet.total_earned.toFixed(2);
      document.getElementById('wallet-breakdown-pending').innerText = data.wallet.pending_earnings.toFixed(2);
      document.getElementById('wallet-breakdown-withdrawn').innerText = data.wallet.total_withdrawn.toFixed(2);
      document.getElementById('modal-withdraw-avail').innerText = data.wallet.balance.toFixed(2);

      // Render transactions table
      renderTransactionsTable(data.transactions);
      // Render withdrawals table
      renderWithdrawalsTable(data.withdrawals);
    }
  } catch (err) {
    showToast('Failed to load wallet data', 'error');
  }
}

function renderTransactionsTable(txs) {
  const tbody = document.getElementById('wallet-transactions-tbody');
  if (txs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" class="py-4 text-center text-slate-400">No transactions recorded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = txs.map(t => {
    const amount = Number(t.amount);
    const balanceAfter = Number(t.balance_after);
    const isCredit = amount > 0;
    const sign = isCredit ? '+' : '';
    const colorClass = isCredit ? 'text-emerald-400' : 'text-red-400';
    const amountText = Number.isFinite(amount) ? Math.abs(amount).toFixed(2) : '0.00';
    const balanceText = Number.isFinite(balanceAfter) ? balanceAfter.toFixed(2) : '0.00';
    return `
      <tr class="hover:bg-slate-900/40">
        <td class="py-2.5">
          <div class="font-medium text-slate-200">${escapeHtml(t.description)}</div>
          <div class="text-[10px] text-slate-400">${escapeHtml(t.created_at)}</div>
        </td>
        <td class="py-2.5 text-right font-bold ${colorClass}">
          ${sign}₹${amountText}
        </td>
        <td class="py-2.5 text-right font-mono text-slate-300">
          ₹${balanceText}
        </td>
      </tr>
    `;
  }).join('');
}

function renderWithdrawalsTable(wds) {
  const tbody = document.getElementById('wallet-withdrawals-tbody');
  if (wds.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" class="py-4 text-center text-slate-400">No withdrawal requests yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = wds.map(w => {
    let badge = '';
    const styles = { requested: 'amber', under_review: 'blue', approved: 'emerald', processing: 'purple', paid: 'emerald', rejected: 'red', failed: 'red', reversed: 'red' };
    const statusKey = String(w.status || '');
    const label = { requested: 'Requested', under_review: 'Under Review', approved: 'Approved', processing: 'Processing', paid: 'Paid', rejected: 'Rejected', failed: 'Failed', reversed: 'Reversed' }[statusKey] || statusKey;
    const tone = styles[statusKey] || 'slate';
    badge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-${tone}-500/20 text-${tone}-400 border border-${tone}-500/30">${escapeHtml(label)}</span>`;
    const amount = Number(w.amount);
    const amountText = Number.isFinite(amount) ? amount.toFixed(2) : '0.00';
    const destination = w.currency === 'USDT'
      ? `USDT ${escapeHtml(w.usdt_network)}`
      : `${escapeHtml(w.payout_method)}: ${escapeHtml(w.upi_id || 'Bank')}`;

    return `
      <tr class="hover:bg-slate-900/40">
        <td class="py-2.5">
          <div class="font-semibold text-white uppercase">${destination}</div>
          <div class="text-[10px] text-slate-400">${escapeHtml(w.requested_at)}</div>
          ${w.utr_number ? `<div class="text-[10px] font-mono text-emerald-400">UTR: ${escapeHtml(w.utr_number)}</div>` : ''}
          ${w.provider_reference ? `<div class="text-[10px] font-mono text-emerald-400">Provider: ${escapeHtml(w.provider_reference)}</div>` : ''}
          ${w.transaction_hash ? `<div class="text-[10px] font-mono break-all text-emerald-400">TX: ${escapeHtml(w.transaction_hash)}</div>` : ''}
          ${w.usdt_amount ? `<div class="text-[10px] text-emerald-400">${escapeHtml(w.usdt_amount)} USDT @ ₹${escapeHtml(w.usdt_inr_rate)}</div>` : ''}
        </td>
        <td class="py-2.5">${badge}</td>
        <td class="py-2.5 text-right font-bold text-white">
          ₹${amountText}
        </td>
      </tr>
    `;
  }).join('');
}

function openWithdrawModal() {
  if (!currentUser) {
    openAuthModal('login');
    return;
  }
  document.getElementById('modal-withdraw-avail').innerText = currentUser.wallet_balance.toFixed(2);
  document.getElementById('withdraw-amount').value = '';
  loadWithdrawalSettings();
  document.getElementById('modal-withdraw').classList.remove('hidden');
  document.body.classList.add('modal-open');
}

async function loadWithdrawalSettings() {
  try {
    const res = await fetch('/api/withdrawal-settings');
    const data = await res.json();
    if (!data.success) return;
    minimumWithdrawal = Number(data.minimum_withdrawal_amount);
    document.getElementById('withdraw-minimum-label').textContent = `Minimum withdrawal: ₹${minimumWithdrawal.toFixed(2)}`;
    document.getElementById('withdraw-amount').min = String(minimumWithdrawal);
    document.getElementById('withdraw-quick-min').textContent = `₹${minimumWithdrawal}`;
    document.getElementById('withdraw-quick-double').textContent = `₹${minimumWithdrawal * 2}`;
    document.getElementById('withdraw-quick-four').textContent = `₹${minimumWithdrawal * 4}`;
    document.getElementById('withdraw-usdt-network').value = data.usdt_network;
    workerUsdtInrRate = Number(data.usdt_inr_rate);
    updateUsdtEstimate();
  } catch (_) { showToast('Could not load withdrawal settings.', 'error'); }
}

function closeWithdrawModal() {
  document.getElementById('modal-withdraw').classList.add('hidden');
  document.body.classList.remove('modal-open');
}

function setWithdrawAmount(val) {
  document.getElementById('withdraw-amount').value = val;
  updateUsdtEstimate();
}

function setWithdrawMax() {
  if (currentUser) {
    document.getElementById('withdraw-amount').value = Math.floor(currentUser.wallet_balance);
    updateUsdtEstimate();
  }
}

function updateUsdtEstimate() {
  const amount = Number.parseFloat(document.getElementById('withdraw-amount')?.value);
  const rate = workerUsdtInrRate;
  const output = document.getElementById('withdraw-usdt-estimate');
  if (output) output.textContent = amount > 0 && rate > 0 ? `Estimated USDT received: ${(amount / rate).toFixed(6)} USDT (rate ₹${rate}/USDT)` : 'Estimated USDT: —';
}

function togglePayoutMethod(method) {
  document.getElementById('payout-upi-container').classList.toggle('hidden', method !== 'upi');
  document.getElementById('payout-bank-container').classList.toggle('hidden', method !== 'bank');
  document.getElementById('payout-usdt-container').classList.toggle('hidden', method !== 'usdt');
}

async function handleWithdrawSubmit(e) {
  e.preventDefault();
  const amount = parseFloat(document.getElementById('withdraw-amount').value);
  const method = document.querySelector('input[name="withdraw-method"]:checked').value;
  const upi_id = document.getElementById('withdraw-upi').value.trim();
  const bank_account = document.getElementById('withdraw-acc').value.trim();
  const bank_ifsc = document.getElementById('withdraw-ifsc').value.trim();
  const bank_holder_name = document.getElementById('withdraw-holder').value.trim();
  const usdt_network = document.getElementById('withdraw-usdt-network').value.trim();
  const usdt_wallet_address = document.getElementById('withdraw-usdt-address').value.trim();


  try {
    const res = await fetch('/api/wallet/withdraw', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        amount,
        payout_method: method,
        upi_id,
        bank_account,
        bank_ifsc,
        bank_holder_name,
        usdt_network,
        usdt_wallet_address
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      closeWithdrawModal();
      await checkAuth();
      loadWalletData();
    } else {
      showToast(data.message || 'Withdrawal failed', 'error');
    }
  } catch (err) {
    showToast('Error requesting withdrawal', 'error');
  }
}

// ================= ADMIN PANEL =================
async function loadAdminData() {
  try {
    // 1. Stats & Settings
    const statsRes = await fetch('/api/admin/stats');
    const statsData = await statsRes.json();
    if (statsData.success) {
      document.getElementById('admin-stat-users').innerText = statsData.stats.total_users;
      document.getElementById('admin-stat-pending-subs').innerText = statsData.stats.pending_submissions;
      document.getElementById('admin-stat-pending-wds').innerText = statsData.stats.pending_withdrawals;
      document.getElementById('admin-stat-disbursed').innerText = statsData.stats.total_disbursed.toFixed(2);
    }
    loadAdminSettings();

    // 2. Load Submissions
    loadAdminSubmissions();
    // 3. Load Withdrawals
    loadAdminWithdrawals();
    // 4. Load Tasks
    loadAdminTasks();
    // 5. Load Users
    loadAdminUsers();
  } catch (err) {
    showToast('Error loading admin control data', 'error');
  }
}

function switchAdminTab(tab) {
  activeAdminTab = tab;
  document.querySelectorAll('.admin-tab').forEach(b => {
    b.classList.remove('text-emerald-400', 'border-emerald-400');
    b.classList.add('text-slate-400', 'border-transparent');
  });
  document.querySelectorAll('.admin-subpanel').forEach(p => p.classList.add('hidden'));

  const activeBtn = document.getElementById(`admin-tab-${tab}`);
  if (activeBtn) {
    activeBtn.classList.add('text-emerald-400', 'border-emerald-400');
    activeBtn.classList.remove('text-slate-400', 'border-transparent');
  }

  const activePanel = document.getElementById(`admin-panel-${tab}`);
  if (activePanel) activePanel.classList.remove('hidden');
}

async function loadAdminSubmissions() {
  try {
    const res = await fetch('/api/admin/submissions');
    const data = await res.json();
    if (!data.success) return;

    const tbody = document.getElementById('admin-submissions-tbody');
    if (data.submissions.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="py-6 text-center text-slate-400">No submissions recorded.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.submissions.map(s => {
      let statusBadge = '';
      let actionButtons = '';

      if (s.status === 'pending') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-amber-500/20 text-amber-400 border border-amber-500/30">Pending</span>`;
        actionButtons = `
          <div class="flex items-center justify-end gap-1.5">
            <button onclick="approveSubmission(${Number(s.id)})" class="px-3 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs shadow-md glow-wa-sm transition flex items-center gap-1">
              <i class="fa-solid fa-check"></i> Full Approve
            </button>
            <button onclick="rejectSubmission(${Number(s.id)})" class="p-1.5 rounded-lg bg-red-500/20 hover:bg-red-500 text-red-300 hover:text-white font-bold text-xs transition" title="Reject">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </div>
        `;
      } else if (s.status === 'approved') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">Approved</span>`;
        actionButtons = `<span class="text-[11px] text-emerald-400 font-semibold">Credited ₹${s.payout_amount.toFixed(2)}</span>`;
      } else {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-red-500/20 text-red-400 border border-red-500/30">Rejected</span>`;
        actionButtons = `<span class="text-[11px] text-red-400">Rejected</span>`;
      }

      // Format AI OCR Audit Tag
      let auditTagHtml = '';
      const badgeText = s.audit_badge || 'Audit Pending';
      const isVerified = badgeText.includes('Auto-Verified');

      if (isVerified) {
        auditTagHtml = `
          <div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 whitespace-nowrap">
            <i class="fa-solid fa-robot text-xs text-emerald-400"></i>
            <span>${escapeHtml(badgeText)}</span>
            ${s.auto_approved ? '<span class="ml-1 text-[9px] uppercase px-1 py-0.2 rounded bg-emerald-950 text-emerald-300 border border-emerald-500/40">Auto</span>' : ''}
          </div>
        `;
      } else {
        auditTagHtml = `
          <div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-bold bg-amber-500/15 text-amber-400 border border-amber-500/30 whitespace-nowrap">
            <i class="fa-solid fa-triangle-exclamation text-xs text-amber-400"></i>
            <span>${escapeHtml(badgeText)}</span>
          </div>
        `;
      }

      submissionProofUrls[s.id] = s.proof_urls?.length ? s.proof_urls : [s.proof_url];
      const encodedCaption = encodeURIComponent(`${s.user_name} - ${s.task_title}`);
      const encodedBadge = encodeURIComponent(String(s.audit_badge || ''));

      return `
        <tr class="hover:bg-slate-900/40">
          <td class="py-3 font-mono font-bold text-slate-300">#${escapeHtml(s.id)}</td>
          <td class="py-3">
            <div class="font-bold text-white">${escapeHtml(s.user_name)}</div>
            <div class="text-[10px] text-slate-400">+91 ${escapeHtml(s.user_mobile)}</div>
          </td>
          <td class="py-3 max-w-[180px]">
            <div class="font-medium text-slate-200 truncate">${escapeHtml(s.task_title)}</div>
            <div class="text-[10px] text-slate-400">${escapeHtml(s.submitted_at)}</div>
          </td>
          <td class="py-3 font-bold text-emerald-400 font-heading">
            ₹${Number(s.payout_amount).toFixed(2)}
          </td>
          <td class="py-3">
            ${auditTagHtml}
          </td>
          <td class="py-3">
            <button data-submission-id="${escapeHtml(s.id)}" data-caption="${escapeHtml(encodedCaption)}" data-payout="${escapeHtml(s.payout_amount)}" data-status="${escapeHtml(s.status)}" data-audit-badge="${escapeHtml(encodedBadge)}" data-audit-score="${escapeHtml(s.audit_score ?? 0)}" data-num-found="${escapeHtml(s.numbers_detected ?? 0)}" data-num-req="${escapeHtml(s.numbers_required ?? 0)}" data-invalid-count="${escapeHtml(s.invalid_count ?? 0)}" onclick="openSubmissionLightboxFromButton(this)" class="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-emerald-400 text-xs border border-slate-700 transition">
              <i class="fa-regular fa-image"></i>
              <span>Inspect (${escapeHtml(s.proof_count || 1)})</span>
            </button>
          </td>
          <td class="py-3">${statusBadge}</td>
          <td class="py-3 text-right">${actionButtons}</td>
        </tr>
      `;
    }).join('');

  } catch (err) {
    console.error('Error loading admin submissions:', err);
  }
}

function openSubmissionLightboxFromButton(button) {
  const decode = value => {
    try { return decodeURIComponent(value || ''); } catch (_) { return ''; }
  };
  openSubmissionLightbox(
    Number(button.dataset.submissionId),
    decode(button.dataset.caption),
    Number(button.dataset.payout),
    button.dataset.status || '',
    decode(button.dataset.auditBadge),
    Number(button.dataset.auditScore || 0),
    Number(button.dataset.numFound || 0),
    Number(button.dataset.numReq || 0),
    Number(button.dataset.invalidCount || 0)
  );
}

async function approveSubmission(subId) {
  try {
    const res = await fetch(`/api/admin/submissions/${subId}/approve`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      closeLightbox();
      loadAdminData();
    } else {
      showToast(data.message || 'Error approving submission', 'error');
    }
  } catch (err) {
    showToast('Failed to approve submission', 'error');
  }
}

function updatePartialPayout(input, subId, fullPayout, requiredCount) {
  const validCount = Math.max(0, Math.min(requiredCount, Number.parseInt(input.value, 10) || 0));
  input.value = validCount;
  const amount = (fullPayout / requiredCount) * validCount;
  const button = document.getElementById(`partial-approve-${subId}`);
  if (button) {
    button.innerHTML = `<i class="fa-solid fa-check"></i> Approve ${validCount}/${requiredCount} & Credit ₹${amount.toFixed(2)}`;
  }
}

async function approvePartialSubmission(subId) {
  const countInput = document.getElementById(`partial-count-${subId}`);
  const validCount = Number.parseInt(countInput?.value, 10);
  try {
    const res = await fetch(`/api/admin/submissions/${subId}/approve-partial`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ valid_count: validCount })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      closeLightbox();
      loadAdminData();
    } else {
      showToast(data.message || 'Error approving partial payment', 'error');
    }
  } catch (err) {
    showToast('Failed to approve partial payment', 'error');
  }
}

async function rejectSubmission(subId) {
  const reason = prompt('Please enter rejection reason (e.g., Unclear screenshot or incomplete messages):', 'Screenshot unclear or invalid message count.');
  if (reason === null) return;

  try {
    const res = await fetch(`/api/admin/submissions/${subId}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'info');
      closeLightbox();
      loadAdminData();
    } else {
      showToast(data.message || 'Error rejecting submission', 'error');
    }
  } catch (err) {
    showToast('Failed to reject submission', 'error');
  }
}

async function loadAdminWithdrawals() {
  try {
    const res = await fetch('/api/admin/withdrawals');
    const data = await res.json();
    if (!data.success) return;

    const tbody = document.getElementById('admin-withdrawals-tbody');
    if (data.withdrawals.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="py-6 text-center text-slate-400">No withdrawal requests found.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.withdrawals.map(w => {
      let statusBadge = '';
      let actionButtons = '';

      if (w.status === 'requested') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-amber-500/20 text-amber-400 border border-amber-500/30">Requested</span>`;
        actionButtons = `
          <div class="flex items-center justify-end gap-1.5">
            <button onclick="transitionWithdrawal(${Number(w.id)}, 'under-review')" class="px-2.5 py-1 rounded bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs transition">
              Review
            </button>
            <button onclick="rejectWithdrawal(${Number(w.id)})" class="px-2 py-1 rounded bg-red-500/20 hover:bg-red-500 text-red-300 hover:text-white font-bold text-xs transition">
              Reject
            </button>
          </div>
        `;
      } else if (w.status === 'under_review') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-blue-500/20 text-blue-400 border border-blue-500/30">Under Review</span>`;
        actionButtons = `<button onclick="transitionWithdrawal(${Number(w.id)}, 'approve')" class="px-2.5 py-1 rounded bg-emerald-500 text-slate-950 font-bold text-xs">Approve</button>`;
      } else if (w.status === 'approved') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">Approved</span>`;
        actionButtons = `<button onclick="transitionWithdrawal(${Number(w.id)}, 'processing')" class="px-2.5 py-1 rounded bg-amber-500 text-slate-950 font-bold text-xs">Start payout</button>`;
      } else if (w.status === 'processing') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-purple-500/20 text-purple-300 border border-purple-500/30">Processing</span>`;
        actionButtons = `<button data-withdrawal-id="${escapeHtml(w.id)}" data-currency="${escapeHtml(w.currency || 'INR')}" onclick="approveWithdrawalFromButton(this)" class="px-2.5 py-1 rounded bg-emerald-500 text-slate-950 font-bold text-xs">Record paid</button>`;
      } else if (w.status === 'paid') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">Paid</span>`;
        actionButtons = `<span class="text-[10px] font-mono text-emerald-400">${w.currency === 'USDT' ? 'USDT paid' : `UTR: ${escapeHtml(w.utr_number || 'Recorded')}`}</span>`;
      } else {
        statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-red-500/20 text-red-400 border border-red-500/30">Rejected</span>`;
        actionButtons = `<span class="text-[10px] text-red-400">Refunded</span>`;
      }

      const copyValue = value => encodeURIComponent(String(value)).replace(/'/g, '%27');
      const dest = w.currency === 'USDT'
        ? `<div>USDT ${escapeHtml(w.usdt_amount)} @ ₹${escapeHtml(w.usdt_inr_rate)}</div><div>${escapeHtml(w.usdt_network)}</div><div class="break-all">${escapeHtml(w.usdt_wallet_address || 'Not available')}</div>${w.usdt_wallet_address ? `<button onclick="copyEncodedText('${copyValue(w.usdt_wallet_address)}', 'Wallet address copied')" class="text-emerald-400">Copy Address</button>` : ''}`
        : (w.payout_method === 'upi' ? `UPI: ${escapeHtml(w.upi_id)}` : `A/C: ${escapeHtml(w.bank_account)} (${escapeHtml(w.bank_ifsc)})`);
      const references = w.currency === 'USDT'
        ? `<div class="text-[10px] text-slate-400">Provider: ${escapeHtml(w.provider_reference || 'Not available')}${w.provider_reference ? ` <button onclick="copyEncodedText('${copyValue(w.provider_reference)}', 'Provider reference copied')" class="text-emerald-400">Copy</button>` : ''}<br>TX: ${escapeHtml(w.transaction_hash || 'Not available')}${w.transaction_hash ? ` <button onclick="copyEncodedText('${copyValue(w.transaction_hash)}', 'Transaction hash copied')" class="text-emerald-400">Copy</button>` : ''}</div>`
        : '';

      return `
        <tr class="hover:bg-slate-900/40">
          <td class="py-3 font-mono font-bold text-slate-300">#${escapeHtml(w.id)}</td>
          <td class="py-3">
            <div class="font-bold text-white">${escapeHtml(w.user_name)}</div>
            <div class="text-[10px] text-slate-400">+91 ${escapeHtml(w.user_mobile)}</div>
          </td>
          <td class="py-3 font-bold text-emerald-400 font-heading">
            ₹${Number(w.amount).toFixed(2)}
          </td>
          <td class="py-3 font-mono text-xs text-slate-200">
            ${dest}
          </td>
          <td class="py-3 text-[10px] text-slate-400">
            ${escapeHtml(w.requested_at)}${references}
          </td>
          <td class="py-3">${statusBadge}</td>
          <td class="py-3 text-right">${actionButtons}</td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    console.error('Error loading admin withdrawals:', err);
  }
}

function approveWithdrawalFromButton(button) {
  approveWithdrawal(Number(button.dataset.withdrawalId), button.dataset.currency || 'INR');
}

async function approveWithdrawal(wdId, currency = 'INR') {
  const provider_reference = prompt('Enter provider/manual payout reference (optional for USDT):') || '';
  const transaction_hash = currency === 'USDT' ? prompt('Enter blockchain transaction hash (required):') : '';
  if (currency === 'USDT' && !transaction_hash?.trim()) return;
  if (currency !== 'USDT' && !provider_reference.trim()) return;

  try {
    const res = await fetch(`/api/admin/withdrawals/${wdId}/paid`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider_reference, transaction_hash })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      loadAdminData();
    } else {
      showToast(data.message || 'Error approving withdrawal', 'error');
    }
  } catch (err) {
    showToast('Failed to approve withdrawal', 'error');
  }
}

async function transitionWithdrawal(wdId, transition) {
  try {
    const res = await fetch(`/api/admin/withdrawals/${wdId}/${transition}`, { method: 'POST' });
    const data = await res.json();
    if (!data.success) throw new Error(data.message);
    showToast(data.message, 'success');
    loadAdminData();
  } catch (err) {
    showToast(err.message || 'Unable to update withdrawal', 'error');
  }
}

async function rejectWithdrawal(wdId) {
  const reason = prompt('Reason for rejecting withdrawal (Amount will be refunded to user):', 'Invalid UPI ID or Bank account.');
  if (reason === null) return;

  try {
    const res = await fetch(`/api/admin/withdrawals/${wdId}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'info');
      loadAdminData();
    } else {
      showToast(data.message || 'Error rejecting withdrawal', 'error');
    }
  } catch (err) {
    showToast('Failed to reject withdrawal', 'error');
  }
}

async function loadAdminTasks() {
  try {
    const res = await fetch('/api/tasks');
    const data = await res.json();
    if (!data.success) return;

    const tbody = document.getElementById('admin-tasks-tbody');
    tbody.innerHTML = data.tasks.map(t => {
      const isAct = t.status === 'active';
      return `
        <tr class="hover:bg-slate-900/40">
          <td class="py-3 font-mono font-bold text-slate-300">#${escapeHtml(t.id)}</td>
          <td class="py-3 font-semibold text-white">${escapeHtml(t.title)}</td>
          <td class="py-3 text-slate-300">${escapeHtml(t.category)}</td>
          <td class="py-3 font-bold text-emerald-400">₹${Number(t.payout).toFixed(2)}</td>
          <td class="py-3 font-mono text-slate-400">${escapeHtml(t.target_count)} Numbers</td>
          <td class="py-3 text-slate-300">${escapeHtml(t.completed_slots)} / ${escapeHtml(t.total_slots)}</td>
          <td class="py-3">
            <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase ${isAct ? 'bg-emerald-500/20 text-emerald-400' : 'bg-slate-800 text-slate-400'}">
              ${escapeHtml(t.status)}
            </span>
          </td>
          <td class="py-3 text-right">
            <button onclick="toggleTaskStatus(${Number(t.id)})" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-xs font-semibold text-slate-300 transition">
              ${isAct ? 'Pause' : 'Activate'}
            </button>
          </td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    console.error(err);
  }
}

async function toggleTaskStatus(taskId) {
  try {
    const res = await fetch(`/api/admin/tasks/${taskId}/toggle`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      loadAdminTasks();
    }
  } catch (err) {
    showToast('Failed to toggle task', 'error');
  }
}

async function loadAdminUsers() {
  try {
    const res = await fetch('/api/admin/users');
    const data = await res.json();
    if (!data.success) return;

    const tbody = document.getElementById('admin-users-tbody');
    tbody.innerHTML = data.users.map(u => {
      return `
        <tr class="hover:bg-slate-900/40">
          <td class="py-3 font-mono font-bold text-slate-300">#${escapeHtml(u.id)}</td>
          <td class="py-3">
            <div class="font-bold text-white">${escapeHtml(u.name)}</div>
            <div class="text-[10px] text-slate-400">+91 ${escapeHtml(u.mobile)} • ${escapeHtml(u.email)}</div>
          </td>
          <td class="py-3 uppercase text-[10px] font-bold ${u.role === 'admin' ? 'text-purple-400' : 'text-slate-300'}">${escapeHtml(u.role)}</td>
          <td class="py-3 font-bold text-emerald-400">₹${Number(u.wallet_balance).toFixed(2)}</td>
          <td class="py-3 text-white font-semibold">₹${Number(u.total_earned).toFixed(2)}</td>
          <td class="py-3 text-center">${escapeHtml(u.approved_tasks)}</td>
          <td class="py-3 text-center">${escapeHtml(u.total_referrals)}</td>
          <td class="py-3 text-[10px] text-slate-400">${escapeHtml(u.created_at)}</td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    console.error(err);
  }
}

document.addEventListener('wheel', event => { const target = event.target; if (target instanceof HTMLInputElement && target.type === 'number' && document.activeElement === target) target.blur(); }, { passive: true });

let createTaskInFlight = false;

function openCreateTaskModal() {
  document.getElementById('modal-create-task').classList.remove('hidden');
  document.body.classList.add('modal-open');
}

function closeCreateTaskModal() {
  document.getElementById('modal-create-task').classList.add('hidden');
  document.body.classList.remove('modal-open');
}

async function handleCreateTaskSubmit(e) {
  e.preventDefault();
  const formElement = e.currentTarget;
  if (createTaskInFlight || !formElement.reportValidity()) return;
  const title = document.getElementById('newtask-title').value.trim();
  const category = document.getElementById('newtask-category').value;
  const reward = document.getElementById('newtask-reward').value;
  const useDefaultReward = document.getElementById('newtask-use-default').checked;
  const imageTask = category === 'IMAGE_TEXT_LINK';
  const description = document.getElementById('newtask-desc').value.trim();
  const template_text = document.getElementById('newtask-template').value.trim();
  const target_numbers = document.getElementById('newtask-numbers').value.trim();
  const total_slots = parseInt(document.getElementById('newtask-slots').value) || 50;
  const recipients_per_slot = parseInt(document.getElementById('newtask-recipients-per-slot').value) || 25;
  const time_limit_minutes = parseInt(document.getElementById('newtask-timelimit').value) || 15;
  const proof_instructions = document.getElementById('newtask-proof').value.trim();
  const pastedNumberCount = target_numbers.split(/[\n,]+/).map(value => value.trim()).filter(Boolean).length;
  const requiredNumbers = total_slots * recipients_per_slot;
  if (!confirm(`Publish this task?\n\nTotal slots: ${total_slots}\nRecipients per slot: ${recipients_per_slot}\nRequired numbers: ${requiredNumbers}\nTarget numbers pasted: ${pastedNumberCount}`) || createTaskInFlight) return;
  createTaskInFlight = true;
  const submitButton = formElement.querySelector('button[type="submit"]');
  const originalLabel = submitButton ? submitButton.textContent : '';
  if (submitButton) { submitButton.disabled = true; submitButton.textContent = 'Publishing…'; }

  try {
    const form = new FormData();
    form.set('title', title); form.set('category', category); form.set('task_type', imageTask ? 'IMAGE_TEXT_LINK' : 'TEXT_LINK');
    form.set('description', description); form.set('template_text', template_text); form.set('target_numbers', target_numbers);
    form.set('total_slots', total_slots); form.set('recipients_per_slot', recipients_per_slot); form.set('time_limit_minutes', time_limit_minutes);
    form.set('proof_instructions', proof_instructions); form.set('use_default_reward', useDefaultReward ? '1' : '0');
    if (reward) form.set('reward_per_number', reward);
    if (imageTask) { form.set('destination_link', document.getElementById('newtask-link').value.trim()); const image = document.getElementById('newtask-image').files[0]; if (image) form.set('campaign_image', image); }
    const res = await fetch('/api/admin/tasks/create', {
      method: 'POST',
      body: form
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      closeCreateTaskModal();
      loadAdminTasks();
      loadTasks();
    } else {
      createTaskInFlight = false;
      if (submitButton) { submitButton.disabled = false; submitButton.textContent = originalLabel; }
      showToast(data.message || 'Error creating task', 'error');
    }
  } catch (err) {
    createTaskInFlight = false;
    if (submitButton) { submitButton.disabled = false; submitButton.textContent = originalLabel; }
    showToast('Failed to publish task', 'error');
  }
}

function updateCreateTaskTypeFields() {
  const image = document.getElementById('newtask-category')?.value === 'IMAGE_TEXT_LINK';
  const fields = document.getElementById('newtask-image-fields');
  if (!fields) return;
  fields.classList.toggle('hidden', !image);
  document.getElementById('newtask-image').required = image;
  document.getElementById('newtask-link').required = image;
}
document.getElementById('newtask-category')?.addEventListener('change', updateCreateTaskTypeFields);
updateCreateTaskTypeFields();

// ================= LIGHTBOX PREVIEW =================
function openSubmissionLightbox(subId, caption, payout, status, auditBadge = '', auditScore = 95, numFound = 0, numReq = 0, invalidCount = 0) {
  openLightbox(submissionProofUrls[subId] || [], caption, subId, payout, status, auditBadge, auditScore, numFound, numReq, invalidCount);
}

function openLightbox(proofUrls, caption, subId, payout, status, auditBadge = '', auditScore = 95, numFound = 0, numReq = 0, invalidCount = 0) {
  const safeStatus = escapeHtml(status);
  activeLightboxProofs = Array.isArray(proofUrls) ? proofUrls : [proofUrls];
  selectLightboxProof(0);
  document.getElementById('lightbox-caption').innerText = caption;

  // Populate AI OCR Audit Card
  const badgeEl = document.getElementById('lightbox-audit-badge');
  const numbersCountEl = document.getElementById('lightbox-numbers-count');
  const templateMatchEl = document.getElementById('lightbox-template-match');
  const summaryEl = document.getElementById('lightbox-audit-summary');

  const badgeText = auditBadge || (numReq > 0 ? `Auto-Verified: ${numFound}/${numReq} numbers found (Match: ${auditScore}%)` : 'AI Audit Complete');
  const isVerified = badgeText.includes('Auto-Verified');

  if (badgeEl) {
    badgeEl.innerText = badgeText;
    badgeEl.className = isVerified
      ? 'px-2.5 py-0.5 rounded text-[11px] font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
      : 'px-2.5 py-0.5 rounded text-[11px] font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30';
  }

  if (numbersCountEl) {
    const total = numReq || 25;
    const found = numFound;
    const pct = Math.round((found / total) * 100);
    numbersCountEl.innerHTML = `Numbers Count: <b class="text-emerald-400">${found}/${total} (${pct}%)</b>`;
  }

  if (templateMatchEl) {
    templateMatchEl.innerHTML = `Template Match: <b class="text-emerald-400">${auditScore}%</b>`;
  }

  if (summaryEl) {
    summaryEl.innerText = isVerified
      ? 'Native AI OCR verified message contents, recipient contact numbers, and delivery confirmation ticks.'
      : 'Some numbers were missing or template text had differences. Please verify the screenshot preview above.';
    if (invalidCount > 0) {
      summaryEl.innerText += ` ${invalidCount} number${invalidCount === 1 ? ' was' : 's were'} marked invalid by WhatsApp and excluded from the required count.`;
    }
  }

  const actions = document.getElementById('lightbox-primary-actions');
  if (status === 'pending') {
    const requiredCount = Math.max(1, numReq);
    const defaultValidCount = Math.min(Math.max(0, numFound), requiredCount);
    const defaultAmount = (payout / requiredCount) * defaultValidCount;
    actions.innerHTML = `
      <div class="flex-1 flex flex-col gap-2">
        <label class="text-[11px] text-slate-400 font-semibold">Valid messages to pay (AI found ${numFound}/${requiredCount})</label>
        <div class="flex items-center gap-2">
          <input id="partial-count-${subId}" type="number" min="0" max="${requiredCount}" value="${defaultValidCount}" oninput="updatePartialPayout(this, ${subId}, ${payout}, ${requiredCount})" class="w-20 rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm font-bold text-white focus:outline-none focus:border-emerald-400" aria-label="Valid message count">
          <span class="text-xs text-slate-400">of ${requiredCount}${invalidCount ? ` (${invalidCount} invalid excluded)` : ''}</span>
        </div>
        <button id="partial-approve-${subId}" onclick="approvePartialSubmission(${subId})" class="w-full py-2.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs shadow-md glow-wa transition flex items-center justify-center gap-1.5">
          <i class="fa-solid fa-check"></i> Approve ${defaultValidCount}/${requiredCount} & Credit ₹${defaultAmount.toFixed(2)}
        </button>
      </div>
      <button onclick="rescanSubmissionOCR(${subId})" class="px-3.5 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 font-bold text-xs transition border border-slate-700 flex items-center gap-1" title="Re-run OCR">
        <i class="fa-solid fa-rotate text-emerald-400"></i> Re-Scan
      </button>
      <button onclick="rejectSubmission(${subId})" class="px-4 py-2.5 rounded-xl bg-red-500/20 hover:bg-red-500 text-red-300 hover:text-white font-bold text-xs transition">
        <i class="fa-solid fa-xmark mr-1"></i> Reject
      </button>
    `;
  } else {
    actions.innerHTML = `
      <div class="text-center w-full text-xs text-slate-400 py-1">
        Status: <span class="font-bold uppercase text-emerald-400">${safeStatus}</span>
        ${status === 'approved' ? `(₹${payout.toFixed(2)} Credited)` : ''}
      </div>
    `;
  }

  document.getElementById('modal-lightbox').classList.remove('hidden');
  document.body.classList.add('modal-open');
  document.addEventListener('keydown', closeLightboxOnEscape);
}

function selectLightboxProof(index) {
  if (!activeLightboxProofs.length) return;
  const activeIndex = Math.max(0, Math.min(index, activeLightboxProofs.length - 1));
  const image = document.getElementById('lightbox-img');
  if (image) image.src = activeLightboxProofs[activeIndex];
  const gallery = document.getElementById('lightbox-gallery');
  if (!gallery) return;
  gallery.replaceChildren();
  activeLightboxProofs.forEach((url, imageIndex) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `relative w-16 h-16 shrink-0 rounded-lg overflow-hidden border ${imageIndex === activeIndex ? 'border-emerald-400' : 'border-slate-700'}`;
    button.onclick = () => selectLightboxProof(imageIndex);
    const thumb = document.createElement('img');
    thumb.src = url;
    thumb.alt = `Screenshot ${imageIndex + 1}`;
    thumb.className = 'w-full h-full object-cover';
    const label = document.createElement('span');
    label.className = 'absolute bottom-0 inset-x-0 bg-slate-950/85 text-[10px] text-white';
    label.textContent = imageIndex + 1;
    button.append(thumb, label);
    gallery.appendChild(button);
  });
}

function closeLightboxOnEscape(event) {
  if (event.key === 'Escape') closeLightbox();
}

function closeLightbox() {
  document.getElementById('modal-lightbox').classList.add('hidden');
  document.body.classList.remove('modal-open');
  document.removeEventListener('keydown', closeLightboxOnEscape);
}

async function rescanSubmissionOCR(subId) {
  try {
    const res = await fetch(`/api/admin/submissions/${subId}/rescan`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      closeLightbox();
      loadAdminSubmissions();
    } else {
      showToast(data.message || 'Re-scan failed', 'error');
    }
  } catch (err) {
    showToast('Failed to re-scan screenshot', 'error');
  }
}

// ================= ADMIN SETTINGS (AUTO-APPROVE) =================
async function loadAdminSettings() {
  try {
    const res = await fetch('/api/admin/settings');
    const data = await res.json();
    if (data.success) {
      updateAutoApproveUI(data.auto_approve_enabled);
      document.getElementById('per-message-rate').value = data.per_message_rate;
      document.getElementById('referral-limit-tasks').value = data.referral_limit_tasks;
      document.getElementById('referral-commission-percent').value = data.referral_commission_percent;
      document.getElementById('minimum-withdrawal-amount').value = data.minimum_withdrawal_amount;
      document.getElementById('usdt-inr-rate').value = data.usdt_inr_rate;
      document.getElementById('withdraw-usdt-network').value = data.usdt_network;
    }
  } catch (err) {
    console.error('Error loading admin settings:', err);
  }
}

function updateAutoApproveUI(isEnabled) {
  const label = document.getElementById('auto-approve-label');
  const dot = document.getElementById('auto-approve-status-dot');
  const btn = document.getElementById('btn-toggle-auto-approve');
  if (!label || !dot || !btn) return;

  if (isEnabled) {
    label.innerText = 'ON (Instant Auto-Credit)';
    dot.className = 'w-2 h-2 rounded-full bg-emerald-400 live-dot';
    btn.className = 'px-3.5 py-1.5 rounded-xl text-xs font-bold transition flex items-center gap-2 bg-emerald-950 border border-emerald-500/50 text-emerald-300';
  } else {
    label.innerText = 'OFF (Manual 1-Click)';
    dot.className = 'w-2 h-2 rounded-full bg-slate-500';
    btn.className = 'px-3.5 py-1.5 rounded-xl text-xs font-bold transition flex items-center gap-2 bg-slate-800 text-slate-300 border border-slate-700';
  }
}

async function savePaymentSettings() {
  const per_message_rate = Number.parseFloat(document.getElementById('per-message-rate').value);
  const referral_limit_tasks = Number.parseInt(document.getElementById('referral-limit-tasks').value, 10);
  const referral_commission_percent = Number.parseFloat(document.getElementById('referral-commission-percent').value);
  const minimum_withdrawal_amount = Number.parseFloat(document.getElementById('minimum-withdrawal-amount').value);
  const usdt_inr_rate = Number.parseFloat(document.getElementById('usdt-inr-rate').value);
  try {
    const res = await fetch('/api/admin/settings/payment', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ per_message_rate, referral_limit_tasks, referral_commission_percent, minimum_withdrawal_amount, usdt_inr_rate })
    });
    const data = await res.json();
    if (!data.success) throw new Error(data.message);
    showToast(data.message, 'success');
    loadTasks();
  } catch (err) {
    showToast(err.message || 'Failed to save payment settings', 'error');
  }
}

async function toggleAutoApproveSetting() {
  try {
    const res = await fetch('/api/admin/settings/toggle-auto-approve', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'info');
      updateAutoApproveUI(data.auto_approve_enabled);
    }
  } catch (err) {
    showToast('Failed to toggle auto-approval', 'error');
  }
}

// ================= REFERRALS & LEADERBOARD =================
async function loadReferralData() {
  try {
    const res = await fetch('/api/referrals');
    const data = await res.json();
    if (data.success) {
      document.getElementById('referral-code-display').innerText = data.referral_code;
      document.getElementById('referral-count-stat').innerText = data.total_referrals;
      document.getElementById('referral-earnings-stat').innerText = data.total_referral_earnings.toFixed(2);

      const container = document.getElementById('invited-friends-container');
      if (data.friends.length === 0) {
        container.innerHTML = `<p class="text-xs text-slate-400 py-4 text-center">You haven't invited any friends yet. Share your link to earn 10% lifetime referral commission.</p>`;
      } else {
        container.innerHTML = `
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            ${data.friends.map(f => `
              <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 flex items-center justify-between">
                <div>
                <div class="font-bold text-white text-xs">${escapeHtml(f.name)}</div>
                  <div class="text-[10px] text-slate-400">${escapeHtml(f.mobile)} • Joined ${escapeHtml(String(f.joined_at || '').split(' ')[0])}</div>
                </div>
                <span class="text-xs font-bold text-emerald-400">10% commission</span>
              </div>
            `).join('')}
          </div>
        `;
      }
    }
  } catch (err) {
    showToast('Failed to load referral data', 'error');
  }
}

function copyReferralLink() {
  if (!currentUser) return;
  const link = `${window.location.origin}/?ref=${currentUser.referral_code}`;
  navigator.clipboard.writeText(link).then(() => {
    document.getElementById('copy-ref-label').innerText = 'Copied!';
    showToast('Referral link copied!', 'success');
    setTimeout(() => { document.getElementById('copy-ref-label').innerText = 'Copy Link'; }, 2000);
  });
}

function shareReferralWhatsApp() {
  if (!currentUser) return;
  const link = `${window.location.origin}/?ref=${currentUser.referral_code}`;
  const text = encodeURIComponent(`🔥 Earn with simple WhatsApp tasks on your phone! Join TaskPe with my code *${currentUser.referral_code}*. Earn 10% Lifetime Referral Commission: ${link}`);
  window.open(`https://api.whatsapp.com/send?text=${text}`, '_blank');
}

async function loadLeaderboard() {
  try {
    const res = await fetch('/api/leaderboard');
    const data = await res.json();
    if (data.success) {
      const tbody = document.getElementById('leaderboard-tbody');
      tbody.innerHTML = data.leaderboard.map(u => {
        let rankBadge = '';
        if (u.rank === 1) rankBadge = `<span class="text-amber-400 text-base font-black"><i class="fa-solid fa-trophy mr-1"></i> #1</span>`;
        else if (u.rank === 2) rankBadge = `<span class="text-slate-300 text-sm font-bold"><i class="fa-solid fa-medal mr-1"></i> #2</span>`;
        else if (u.rank === 3) rankBadge = `<span class="text-amber-600 text-sm font-bold"><i class="fa-solid fa-medal mr-1"></i> #3</span>`;
        else rankBadge = `<span class="text-slate-400 font-mono font-semibold">#${escapeHtml(u.rank)}</span>`;

        return `
          <tr class="hover:bg-slate-900/40">
            <td class="py-3 text-center">${rankBadge}</td>
            <td class="py-3 font-bold text-white">${escapeHtml(u.name)}</td>
            <td class="py-3 font-mono text-slate-400 text-xs">${escapeHtml(u.mobile)}</td>
            <td class="py-3 text-center font-bold text-slate-300">${escapeHtml(u.tasks_done)}</td>
            <td class="py-3 text-right font-black text-emerald-400 font-heading text-sm">
              ₹${Number(u.total_earned).toFixed(2)}
            </td>
          </tr>
        `;
      }).join('');
    }
  } catch (err) {
    console.error(err);
  }
}

// ================= CALCULATOR =================
function updateCalculator(val) {
  const tasks = parseInt(val);
  const avgPayout = 70;
  const daily = tasks * avgPayout;
  const monthly = daily * 30;

  document.getElementById('calc-tasks-label').innerText = `${tasks} Tasks / Day`;
  document.getElementById('calc-daily-earning').innerText = `₹${daily.toLocaleString('en-IN')}`;
  document.getElementById('calc-monthly-earning').innerText = `₹${monthly.toLocaleString('en-IN')}`;
}

// ================= TOAST SYSTEM =================
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `pointer-events-auto flex items-center gap-3 px-4 py-3 rounded-2xl shadow-2xl border text-xs font-medium transition transform translate-y-2 opacity-0 duration-300 ${
    type === 'success' ? 'bg-slate-900 border-emerald-500/50 text-emerald-300' :
    type === 'error' ? 'bg-slate-900 border-red-500/50 text-red-300' :
    'bg-slate-900 border-slate-700 text-slate-200'
  }`;

  const icon = document.createElement('i');
  icon.className = type === 'success' ? 'fa-solid fa-circle-check text-emerald-400 text-sm' :
                   type === 'error' ? 'fa-solid fa-circle-exclamation text-red-400 text-sm' :
                   'fa-solid fa-circle-info text-blue-400 text-sm';
  const messageNode = document.createElement('span');
  messageNode.textContent = String(message ?? '');
  toast.append(icon, messageNode);
  container.appendChild(toast);

  // Trigger animation
  requestAnimationFrame(() => {
    toast.classList.remove('translate-y-2', 'opacity-0');
  });

  setTimeout(() => {
    toast.classList.add('opacity-0', 'translate-y-2');
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}
