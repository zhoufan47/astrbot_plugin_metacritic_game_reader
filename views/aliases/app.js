/**
 * 别名管理视图的前端逻辑。
 *
 * 与 AstrBot Dashboard 的通信全部通过 `window.AstrBotPluginView` bridge 完成：
 *   - apiGet(endpoint, params)  → GET，params 作为 query
 *   - apiPost(endpoint, body)   → POST JSON
 * 注意：bridge 没有 apiDelete，删除操作通过 POST `aliases/delete` 完成。
 *
 * 后端返回普通 JSON 时 Promise 解析为完整对象；
 * 返回 {status:"error"} 或 HTTP 失败时 Promise reject 为 Error。
 */

const bridge = window.AstrBotPluginView;

const SOURCE_LABEL = { user: "用户添加", llm: "LLM 自动", builtin: "内置表" };
const SOURCE_CLASS = { user: "user", llm: "llm", builtin: "builtin" };

/** 页面状态 */
const state = {
  items: [],
  stats: { total: 0, user: 0, llm: 0, builtin: 0 },
  query: "",
  sourceFilter: "",
  loading: false,
};

const el = {
  title: document.getElementById("title"),
  subtitle: document.getElementById("subtitle"),
  stats: document.getElementById("stats"),
  search: document.getElementById("search"),
  sourceFilter: document.getElementById("source-filter"),
  addForm: document.getElementById("add-form"),
  newAlias: document.getElementById("new-alias"),
  newTitle: document.getElementById("new-title"),
  tbody: document.getElementById("tbody"),
  empty: document.getElementById("empty"),
  loading: document.getElementById("loading"),
  table: document.getElementById("table"),
  path: document.getElementById("path"),
  count: document.getElementById("count"),
  toast: document.getElementById("toast"),
  btnAdd: document.getElementById("btn-add"),
  btnRefresh: document.getElementById("btn-refresh"),
  btnClear: document.getElementById("btn-clear"),
  btnImport: document.getElementById("btn-import"),
  btnSave: document.getElementById("btn-save"),
  btnCancel: document.getElementById("btn-cancel"),
};

let toastTimer = 0;

/** 显示一条短暂提示。 */
function toast(message, isError = false) {
  el.toast.textContent = message;
  el.toast.classList.toggle("error", isError);
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.toast.hidden = true;
  }, 2600);
}

/** 转义文本，避免把用户输入当 HTML 插入。 */
function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (ch) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[ch],
  );
}

/** 秒级时间戳 → 本地日期字符串。 */
function formatTime(seconds) {
  const n = Number(seconds || 0);
  if (!n) return "—";
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

/** 渲染顶部统计卡片。 */
function renderStats() {
  const s = state.stats || {};
  const cards = [
    { label: "总数", value: s.total ?? 0 },
    { label: "用户添加", value: s.user ?? 0 },
    { label: "LLM 自动", value: s.llm ?? 0 },
    { label: "内置表", value: s.builtin ?? 0 },
  ];
  el.stats.innerHTML = cards
    .map(
      (c) =>
        `<div class="stat"><div class="label">${escapeHtml(
          c.label,
        )}</div><div class="value">${escapeHtml(c.value)}</div></div>`,
    )
    .join("");
}

/** 按来源筛选后的列表。 */
function visibleItems() {
  if (!state.sourceFilter) return state.items;
  return state.items.filter((it) => it.source === state.sourceFilter);
}

/** 渲染表格主体。 */
function renderTable() {
  const items = visibleItems();
  const hasAny = items.length > 0;
  el.table.hidden = !hasAny;
  el.empty.hidden = hasAny;

  if (!hasAny) {
    el.tbody.innerHTML = "";
    el.count.textContent = `共 0 条（总计 ${state.stats?.total ?? 0}）`;
    return;
  }

  el.tbody.innerHTML = items
    .map((it) => {
      const src = String(it.source || "");
      const badgeClass = SOURCE_CLASS[src] || "unknown";
      const badgeText = SOURCE_LABEL[src] || src || "未知";
      return `<tr data-alias="${escapeHtml(it.alias)}">
        <td><span class="alias-text">${escapeHtml(it.alias)}</span></td>
        <td><span class="title-text">${escapeHtml(it.title)}</span></td>
        <td><span class="badge ${badgeClass}">${escapeHtml(badgeText)}</span></td>
        <td class="muted">${escapeHtml(it.hits ?? 0)}</td>
        <td class="muted">${escapeHtml(formatTime(it.updated_at))}</td>
        <td class="ops">
          <button class="btn tiny danger-ghost js-del" data-alias="${escapeHtml(
            it.alias,
          )}">删除</button>
        </td>
      </tr>`;
    })
    .join("");

  const total = state.stats?.total ?? items.length;
  el.count.textContent = state.sourceFilter
    ? `显示 ${items.length} 条（总计 ${total}）`
    : `共 ${items.length} 条`;
}

/** 从后端拉取列表。 */
async function load() {
  state.loading = true;
  el.loading.hidden = false;
  try {
    const params = { limit: 200 };
    if (state.query) params.q = state.query;
    const data = await bridge.apiGet("aliases/list", params);
    state.items = Array.isArray(data?.items) ? data.items : [];
    state.stats = data?.stats || { total: 0, user: 0, llm: 0, builtin: 0 };
    if (data?.path) el.path.textContent = `存储位置：${data.path}`;
    renderStats();
    renderTable();
  } catch (error) {
    toast(`加载失败：${error?.message || error}`, true);
  } finally {
    state.loading = false;
    el.loading.hidden = true;
  }
}

/** 新增或更新别名。 */
async function saveAlias(alias, title) {
  el.btnSave.disabled = true;
  try {
    const result = await bridge.apiPost("aliases/save", { alias, title });
    const changed = Boolean(result?.changed);
    toast(changed ? "已保存" : "已存在，内容未变");
    await load();
    return true;
  } catch (error) {
    toast(`保存失败：${error?.message || error}`, true);
    return false;
  } finally {
    el.btnSave.disabled = false;
  }
}

/** 删除别名（bridge 无 DELETE，走 POST）。 */
async function deleteAlias(alias) {
  if (!window.confirm(`确定删除别名「${alias}」？`)) return;
  try {
    const result = await bridge.apiPost("aliases/delete", { alias });
    const removed = Number(result?.deleted ?? 0);
    toast(removed ? `已删除「${alias}」` : `未找到「${alias}」`, !removed);
    await load();
  } catch (error) {
    toast(`删除失败：${error?.message || error}`, true);
  }
}

/** 清空别名（可选按来源）。 */
async function clearAliases() {
  const which = window.prompt(
    "要清空哪些别名？输入：全部 / user / llm / builtin",
    "全部",
  );
  if (which === null) return;
  const value = which.trim();
  if (!value) return;
  const source =
    value === "全部" || value.toLowerCase() === "all"
      ? null
      : ["user", "llm", "builtin"].includes(value.toLowerCase())
        ? value.toLowerCase()
        : "invalid";

  if (source === "invalid") {
    toast("只支持：全部 / user / llm / builtin", true);
    return;
  }
  const label = source ? `来源「${source}」的` : "全部";
  if (!window.confirm(`确定清空${label}别名？此操作不可撤销。`)) return;

  try {
    const result = await bridge.apiPost("aliases/clear", source ? { source } : {});
    toast(`已清空 ${result?.cleared ?? 0} 条`);
    await load();
  } catch (error) {
    toast(`清空失败：${error?.message || error}`, true);
  }
}

/** 把内置别名表导入库中。 */
async function importBuiltin() {
  if (!window.confirm("把内置别名表导入库中？已存在的同名别名会被跳过。")) return;
  try {
    const result = await bridge.apiPost("aliases/import_builtin", {});
    toast(`已导入 ${result?.added ?? 0} 条`);
    await load();
  } catch (error) {
    toast(`导入失败：${error?.message || error}`, true);
  }
}

/** 绑定事件。 */
function bindEvents() {
  // 搜索（防抖）
  let searchTimer = 0;
  el.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = el.search.value.trim();
      load();
    }, 300);
  });

  el.sourceFilter.addEventListener("change", () => {
    state.sourceFilter = el.sourceFilter.value;
    renderTable();
  });

  el.btnAdd.addEventListener("click", () => {
    el.addForm.hidden = !el.addForm.hidden;
    if (!el.addForm.hidden) el.newAlias.focus();
  });

  el.btnCancel.addEventListener("click", () => {
    el.addForm.hidden = true;
    el.addForm.reset();
  });

  el.addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const alias = el.newAlias.value.trim();
    const title = el.newTitle.value.trim();
    if (!alias || !title) {
      toast("别名和官方名都不能为空", true);
      return;
    }
    const ok = await saveAlias(alias, title);
    if (ok) {
      el.addForm.reset();
      el.addForm.hidden = true;
    }
  });

  // 事件委托：删除按钮
  el.tbody.addEventListener("click", (event) => {
    const btn = event.target.closest(".js-del");
    if (!btn) return;
    deleteAlias(btn.dataset.alias || "");
  });

  el.btnRefresh.addEventListener("click", () => load());
  el.btnClear.addEventListener("click", () => clearAliases());
  el.btnImport.addEventListener("click", () => importBuiltin());
}

/** 响应 WebUI 语言 / 主题变化。 */
function applyContext(context) {
  const locale = context?.locale || "zh-CN";
  document.documentElement.lang = locale;
  const primary = context?.themeColors?.primary;
  const secondary = context?.themeColors?.secondary;
  if (primary) document.documentElement.style.setProperty("--primary", primary);
  if (secondary) {
    // 次要色目前不单独使用，保留给后续扩展
    document.documentElement.style.setProperty("--primary-2", secondary);
  }
  el.title.textContent = bridge.t("views.aliases.title", "游戏别名库");
  el.subtitle.textContent = bridge.t(
    "views.aliases.description",
    "查询时优先命中这里的别名；LLM 识别成功后会自动写入。",
  );
  document.title = el.title.textContent;
}

async function main() {
  bindEvents();
  try {
    const context = await bridge.ready();
    applyContext(context);
    bridge.onContext((ctx) => {
      applyContext(ctx);
      // 主题切换后无需重新拉数据，样式由 CSS 变量自动生效
    });
  } catch (error) {
    // 上下文失败不应阻止页面继续工作
    console.warn("bridge.ready failed:", error);
  }
  await load();
}

main();
