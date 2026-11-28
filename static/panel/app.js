'use strict';
/* 管理面板前端（Vue 3 全局构建，随仓库分发，无 CDN 依赖） */
const tg = window.Telegram.WebApp || {};
const API = window.location.pathname.replace(/\/$/, '') + '/api';
// ?debug=1：跳过 initData 门禁渲染完整界面（API 会 401，用于排查渲染问题）
const DEBUG_RENDER = new URLSearchParams(window.location.search).has('debug');

// 页面内显示 JS 错误：Mini App 里看不到控制台，截图即可定位
window.__panelErrors = [];
function showFatal(message) {
  window.__panelErrors.push(String(message));
  const box = document.getElementById('fatal');
  if (box) {
    box.style.display = 'block';
    box.textContent = '⚠️ 脚本错误：' + window.__panelErrors.slice(-3).join('\n');
  }
}
window.addEventListener('error', e => showFatal(e.message || e.type));
window.addEventListener('unhandledrejection', e => showFatal(e.reason?.message || String(e.reason)));

const BOT_FILTERS = [['all', '全部'], ['active', '活跃'], ['revoked', 'Token 失效'], ['banned', '封禁']];
const CODE_FILTERS = [['pending', '待兑换'], ['used', '已兑换'], ['cancelled', '已作废'], ['all', '全部']];
const FILE_TYPES = [['', '全部'], ['photo', '图片'], ['video', '视频'], ['document', '文档'],
                    ['audio', '音频'], ['voice', '语音'], ['animation', '动图']];
const FILE_SORTS = [['new', '最新'], ['old', '最早'], ['size', '最大']];
const TYPE_EMOJI = { photo: '🖼', video: '🎬', document: '📄', audio: '🎵', voice: '🎤', animation: '✨' };
const STATE_COLORS = {
  active: 'green', paused: 'amber', revoked: 'red', banned: 'red',
  admin_stopped: 'red', frozen: 'red', deleted: 'gray',
};
const LEVELS = [
  { value: 1, label: 'VIP 1' }, { value: 2, label: 'VIP 2' },
  { value: 3, label: 'VIP 3' }, { value: 4, label: '基础版' },
];

const Pager = {
  props: { page: Number, last: Number, total: Number, unit: String },
  emits: ['go'],
  template: `
    <div class="bar">
      <button :disabled="page <= 1" @click="$emit('go', page - 1)">‹ 上一页</button>
      <span class="muted">第 {{ page }}/{{ last }} 页 · 共 {{ fmt(total) }} {{ unit }}</span>
      <button :disabled="page >= last" @click="$emit('go', page + 1)">下一页 ›</button>
    </div>`,
  methods: { fmt: fmt },
};

const RowLine = {
  props: { k: String, v: String },
  template: `
    <div class="row"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></div>`,
};

async function api(path, opts = {}) {
  const headers = { 'X-Telegram-Init-Data': tg.initData || (DEBUG_RENDER ? 'debug' : '') };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(API + path, {
    method: opts.method || 'GET', headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await response.json(); } catch (e) { /* 非 JSON 响应体 */ }
  if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
  return data;
}

function fmt(n) { return Number(n || 0).toLocaleString('en-US'); }

function humanSize(n) {
  if (n === null || n === undefined) return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

function confirmDialog(text) {
  return new Promise(resolve => {
    if (tg.showConfirm) tg.showConfirm(text, ok => resolve(Boolean(ok)));
    else resolve(window.confirm(text));
  });
}

Vue.createApp({
  components: { Pager, RowLine },
  data() {
    return {
      ready: false,
      gateText: '正在连接…',
      busy: false,
      updated: '',
      // 概览
      stats: null,
      // Bot 列表
      bots: [], botsLoaded: false, botsTotal: 0, botPage: 1, botLastPage: 1,
      botStatus: 'all', botSearch: '',
      detail: null,  // {botId, username, tab, fileType, sort, page, lastPage, total, files, collections, loading, error}
      // 用户查询
      uidInput: '', userResult: null,
      // 兑换码
      codes: [], codesLoaded: false, codeStatus: 'pending',
      showGen: false, genForm: { level: 1, months: 12, count: 5, expiresDays: '' },
      genResult: null, genError: '', copied: '', revokeBatch: '',
      // 会员
      vips: [], vipsLoaded: false, showVipForm: false,
      vipForm: { userId: '', level: 1, months: 12 }, vipResult: '', vipError: '',
      // 定向赠送
      gift: { group: 'paid', level: 3, months: 1, expiresDays: '', reason: '',
              confirmed: false, selected: [], loading: false, sending: false,
              error: '', result: null, addId: '' },
      // 订单
      orders: [], ordersLoaded: false,
      errors: {},
      // 常量
      botFilters: BOT_FILTERS, codeFilters: CODE_FILTERS,
      fileTypes: FILE_TYPES, fileSorts: FILE_SORTS, levels: LEVELS,
    };
  },
  computed: {
    statCards() {
      if (!this.stats) return [];
      return [
        { label: '运行中 Bot', value: fmt(this.stats.running_bots) },
        { label: '活跃 Bot', value: fmt(this.stats.bot_count) },
        { label: 'Bot 总数', value: fmt(this.stats.total_bots) },
        { label: '用户数', value: fmt(this.stats.owner_count) },
        { label: '文件总数', value: fmt(this.stats.file_count) },
        { label: '集合数', value: fmt(this.stats.col_count) },
        { label: '黑名单', value: fmt(this.stats.blacklist_count) },
      ];
    },
  },
  methods: {
    fmt, humanSize,
    stateColor(status) { return STATE_COLORS[status] || 'gray'; },
    typeEmoji(type) { return TYPE_EMOJI[type] || '📄'; },
    codeStatusLabel(status) {
      return { pending: '待兑换', used: '已兑换', cancelled: '已作废' }[status] || status;
    },
    codeStatusColor(status) {
      return { pending: 'green', used: 'amber', cancelled: 'gray' }[status] || 'gray';
    },
    failedCount(counts) {
      counts = counts || {};
      return (counts.revoked || 0) + (counts.banned || 0)
        + (counts.frozen || 0) + (counts.admin_stopped || 0);
    },
    async run(fn) {
      this.busy = true;
      try { await fn(); } finally { this.busy = false; }
    },
    async loadStats() {
      try {
        this.stats = await api('/stats');
        this.updated = new Date().toLocaleTimeString('zh-CN');
        this.errors.stats = '';
      } catch (e) { this.errors.stats = e.message; }
    },
    async loadBots() {
      try {
        const search = this.botSearch ? `&search=${encodeURIComponent(this.botSearch)}` : '';
        const data = await api(`/bots?status=${this.botStatus}&page=${this.botPage}${search}`);
        this.bots = data.bots; this.botsTotal = data.total;
        this.botPage = data.page; this.botLastPage = data.last_page;
        this.botsLoaded = true; this.errors.bots = '';
      } catch (e) { this.errors.bots = e.message; }
    },
    setBotFilter(value) {
      this.botStatus = value; this.botPage = 1; this.closeDetail(); this.loadBots();
    },
    runSearch() {
      this.botPage = 1; this.closeDetail(); this.loadBots();
    },
    goBots(page) { this.botPage = page; this.closeDetail(); this.loadBots(); },
    toggleDetail(bot) {
      if (this.detail && this.detail.botId === bot.id) { this.detail = null; return; }
      this.detail = { botId: bot.id, username: bot.username, tab: 'files',
                      fileType: '', sort: 'new', page: 1, lastPage: 1, total: 0,
                      files: [], collections: [], loading: false, error: '' };
      this.loadDetail();
    },
    closeDetail() { this.detail = null; },
    setDetailTab(tab) { this.detail.tab = tab; this.detail.page = 1; this.loadDetail(); },
    setFileType(value) { this.detail.fileType = value; this.detail.page = 1; this.loadDetail(); },
    setFileSort(value) { this.detail.sort = value; this.detail.page = 1; this.loadDetail(); },
    goDetailPage(page) { this.detail.page = page; this.loadDetail(); },
    async loadDetail() {
      const detail = this.detail;
      if (!detail) return;
      detail.loading = true; detail.error = '';
      try {
        if (detail.tab === 'files') {
          const type = detail.fileType ? `&file_type=${detail.fileType}` : '';
          const data = await api(`/bot/${detail.botId}/files?sort=${detail.sort}&page=${detail.page}${type}`);
          detail.files = data.files; detail.total = data.total;
          detail.page = data.page; detail.lastPage = data.last_page;
        } else {
          const data = await api(`/bot/${detail.botId}/collections?page=${detail.page}`);
          detail.collections = data.collections; detail.total = data.total;
          detail.page = data.page; detail.lastPage = data.last_page;
        }
      } catch (e) { detail.error = e.message; }
      detail.loading = false;
    },
    async loadUser(uid) {
      if (!uid) return;
      try {
        this.userResult = await api('/user/' + encodeURIComponent(uid));
        this.errors.user = '';
      } catch (e) { this.errors.user = e.message; }
    },
    async loadCodes() {
      try {
        const data = await api('/codes?status=' + this.codeStatus);
        this.codes = data.codes; this.codesLoaded = true; this.errors.codes = '';
      } catch (e) { this.errors.codes = e.message; }
    },
    setCodeFilter(value) { this.codeStatus = value; this.loadCodes(); },
    async createCodes() {
      await this.run(async () => {
        this.genError = '';
        try {
          const data = await api('/codes/generate', { method: 'POST', body: {
            level: this.genForm.level, months: this.genForm.months,
            count: this.genForm.count,
            expires_days: this.genForm.expiresDays === '' ? null : Number(this.genForm.expiresDays),
          } });
          this.genResult = data;
          this.loadCodes();
        } catch (e) { this.genError = e.message; }
      });
    },
    async copyCodes() {
      if (!this.genResult) return;
      const text = this.genResult.codes.join('\n');
      try {
        await navigator.clipboard.writeText(text);
        this.copied = '已复制到剪贴板';
      } catch (e) { this.copied = '长按上方文本手动复制'; }
    },
    async cancelCode(code) {
      const ok = await confirmDialog(`确定作废 ${code}？已生成的码将无法再兑换。`);
      if (!ok) return;
      await this.run(async () => {
        try {
          const data = await api('/codes/cancel', { method: 'POST', body: { code_or_batch: code } });
          tg.showAlert ? tg.showAlert(`已作废 ${data.cancelled} 个码`) : alert(`已作废 ${data.cancelled} 个码`);
          this.loadCodes();
        } catch (e) { this.genError = e.message; }
      });
    },
    async cancelBatch() {
      const batch = this.revokeBatch.trim();
      if (!batch) return;
      const ok = await confirmDialog(`确定作废批次 ${batch} 下全部待兑换码？`);
      if (!ok) return;
      await this.run(async () => {
        try {
          const data = await api('/codes/cancel', { method: 'POST', body: { code_or_batch: batch } });
          tg.showAlert ? tg.showAlert(`已作废 ${data.cancelled} 个码`) : alert(`已作废 ${data.cancelled} 个码`);
          this.revokeBatch = '';
          this.loadCodes();
        } catch (e) { this.genError = e.message; }
      });
    },
    async loadVips() {
      try {
        const data = await api('/vips');
        this.vips = data.members; this.vipsLoaded = true; this.errors.vips = '';
      } catch (e) { this.errors.vips = e.message; }
    },
    async setVip() {
      await this.run(async () => {
        this.vipError = ''; this.vipResult = '';
        try {
          const data = await api('/vip/set', { method: 'POST', body: {
            user_id: Number(this.vipForm.userId), level: this.vipForm.level,
            months: this.vipForm.months,
          } });
          const info = data.info;
          this.vipResult = `已应用：${info.vip_name}（等级 ${info.vip_level}）· ` +
            (info.vip_expire_at ? `到期 ${info.vip_expire_at}` : '永久');
          this.loadVips();
        } catch (e) { this.vipError = e.message; }
      });
    },
    async loadOrders() {
      try {
        const data = await api('/orders');
        this.orders = data.orders; this.ordersLoaded = true; this.errors.orders = '';
      } catch (e) { this.errors.orders = e.message; }
    },
    async loadGiftTargets() {
      this.gift.loading = true; this.gift.error = '';
      try {
        const data = await api(`/gift/targets?group=${this.gift.group}&level=${this.gift.level}`);
        this.gift.selected = data.users.map(u => ({ ...u }));
        this.gift.confirmed = true;
        this.gift.result = null;
      } catch (e) { this.gift.error = e.message; }
      this.gift.loading = false;
    },
    giftAddUser() {
      const uid = Number(this.gift.addId);
      if (!uid || uid <= 0) { this.gift.error = '请输入有效的用户 ID'; return; }
      if (this.gift.selected.some(u => u.user_id === uid)) {
        this.gift.error = `${uid} 已在名单里`; return;
      }
      this.gift.selected.push({ user_id: uid, added: true, pending_same_level: false });
      this.gift.addId = ''; this.gift.error = '';
    },
    async sendGift() {
      await this.run(async () => {
        this.gift.sending = true; this.gift.error = '';
        try {
          const data = await api('/gift/send', { method: 'POST', body: {
            level: this.gift.level, months: this.gift.months,
            expires_days: this.gift.expiresDays === '' ? null : Number(this.gift.expiresDays),
            reason: this.gift.reason,
            users: this.gift.selected.map(u => u.user_id),
          } });
          this.gift.result = data;
          this.loadCodes();
        } catch (e) { this.gift.error = e.message; }
        this.gift.sending = false;
      });
    },
  },
  mounted() {
    try { tg.ready(); tg.expand(); } catch (e) { /* 非 Telegram 环境 */ }
    if (!tg.initData && !DEBUG_RENDER) {
      this.gateText = '请从 Telegram 中打开本页面（管理员命令回复里的「🖥 管理面板」按钮）。';
      return;
    }
    this.ready = true;
    this.loadStats();
    this.loadBots();
    this.loadCodes();
    this.loadVips();
    this.loadOrders();
  },
}).mount('#app');
window.__panelBooted = true;
