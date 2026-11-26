const API_BASE = '/api'

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('bearer_token') || ''
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function fetchJson(path: string) {
  const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`)
  return res.json()
}

async function fetchData(url: string, method: string = 'GET', body: any = null) {
  const options: RequestInit = {
    method,
    headers: {
      ...authHeaders(),
      'Content-Type': 'application/json',
    },
  };
  if (body) {
    options.body = JSON.stringify(body);
  }
  const res = await fetch(`${API_BASE}${url}`, options);
  if (!res.ok) {
    let errorMsg = `${res.status}: ${res.statusText}`;
    try {
      const errorData = await res.json();
      if (errorData.message) {
        errorMsg = errorData.message;
      } else if (errorData.error) {
        errorMsg = errorData.error;
      } else if (errorData.detail) {
        if (typeof errorData.detail === 'string') {
          errorMsg = errorData.detail;
        } else if (Array.isArray(errorData.detail)) {
          errorMsg = errorData.detail.map((d: any) => d.msg || d.message).join('; ');
        }
      }
    } catch (e) {
      // Ignore if JSON parsing fails
    }
    throw new Error(errorMsg);
  }
  return res.json();
}

export const api = {
  acts: () => fetchData('/acts'),
  tree: (act: string) => fetchData(`/tree/${act}`),
  generateMcpToken: () => fetchData('/mcp-token', 'POST', {}),
  listMcpTokens: () => fetchData('/mcp-tokens'),
  revokeMcpToken: (token: string) => fetchData(`/mcp-tokens/${token}/revoke`, 'POST'),
  renameMcpToken: (tokenId: number, name: string) => fetchData(`/mcp-tokens/${tokenId}/rename`, 'POST', { name }),
  section: (act: string, section: string) => fetchJson(`/section/${act}/${section}`),
  cases: (act: string, section: string) => fetchJson(`/cases/${act}/${section}`),
  rulings: (act: string, section: string) => fetchJson(`/rulings/${act}/${section}`),
  definitions: (act: string) => fetchJson(`/definitions/${act}`),
  definitionsAll: () => fetchJson('/definitions'),
  // Gated proxies: fetchData surfaces the backend detail ("Access denied: ...") as the error message.
  gated: (path: string) => fetchData(path),
  definitionsSearch: (act: string, q: string) => fetchJson(`/definitions/${act}/search?q=${encodeURIComponent(q)}`),
  definition: (act: string, term: string) => fetchJson(`/definition/${act}/${term}`),
  definitionText: (act: string, term: string) => fetchJson(`/definition-text/${act}/${term}`),
  search: (q: string, act?: string, offset?: number, limit?: number) => {
    let url = `/search?q=${encodeURIComponent(q)}`
    if (act) url += `&act=${act}`
    if (offset !== undefined) url += `&offset=${offset}`
    if (limit !== undefined) url += `&limit=${limit}`
    return fetchJson(url)
  },
  ruling: (citation: string) => fetchJson(`/ruling/${encodeURIComponent(citation)}`),
  taxCase: (citation: string) => fetchJson(`/tax-cases/case/${encodeURIComponent(citation)}`),
  // Interpretive chains come from tax-encode through the backend; the browser never calls it.
  taxChains: (act: string, section: string) =>
    fetchJson(`/tax/chains/${encodeURIComponent(act)}/${encodeURIComponent(section)}`),
  taxHealth: () => fetchJson('/tax/health'),
  rulingSections: (citation: string) => fetchJson(`/ruling-sections/${encodeURIComponent(citation)}`),
  listComments: (act: string, section: string) => fetchJson(`/comments/${act}/${section}`),
  createComment: (act: string, section: string, author: string, text: string) =>
    fetchData('/comments', 'POST', { act, section, author, text }),
  resolveComment: (commentId: number) =>
    fetchData('/comments/resolve', 'POST', { comment_id: commentId }),
  searchFlat: (q: string, limit?: number) => {
    let url = `/search/flat?q=${encodeURIComponent(q)}`
    if (limit !== undefined) url += `&limit=${limit}`
    return fetchJson(url)
  },
  searchHybrid: (q: string, type?: string, limit?: number, opts?: { operator?: string; dateFrom?: string; dateTo?: string; rtype?: string; outcome?: string; act?: string; scope?: string; yearFrom?: string; yearTo?: string; court?: string; offset?: number }) => {
    let url = `/search/hybrid?q=${encodeURIComponent(q)}`
    if (type) url += `&type=${type}`
    if (limit !== undefined) url += `&limit=${limit}`
    if (opts?.offset) url += `&offset=${opts.offset}`
    if (opts?.act) url += `&act=${encodeURIComponent(opts.act)}`
    if (opts?.scope) url += `&scope=${encodeURIComponent(opts.scope)}`
    if (opts?.operator && opts.operator !== 'AND') url += `&operator=${encodeURIComponent(opts.operator)}`
    if (opts?.dateFrom) url += `&date_from=${encodeURIComponent(opts.dateFrom)}`
    if (opts?.dateTo) url += `&date_to=${encodeURIComponent(opts.dateTo)}`
    if (opts?.rtype) url += `&rtype=${encodeURIComponent(opts.rtype)}`
    if (opts?.outcome) url += `&outcome=${encodeURIComponent(opts.outcome)}`
    if (opts?.yearFrom) url += `&year_from=${encodeURIComponent(opts.yearFrom)}`
    if (opts?.yearTo) url += `&year_to=${encodeURIComponent(opts.yearTo)}`
    if (opts?.court) url += `&court=${encodeURIComponent(opts.court)}`
    return fetchJson(url)
  },
  // Slicer counts over the search's candidate pool; params mirror searchHybrid (act: comma list).
  searchFacets: (q: string, params: Record<string, string | undefined>) => {
    const p = new URLSearchParams({ q })
    for (const [k, v] of Object.entries(params)) if (v) p.set(k, v)
    return fetchJson(`/search/facets?${p}`)
  },
  suggest: (q: string, limit?: number) => {
    let url = `/search/suggest?q=${encodeURIComponent(q)}`
    if (limit !== undefined) url += `&limit=${limit}`
    return fetchJson(url)
  },
  searchWarm: () => fetchJson('/search/warm'),
  info: () => fetchJson('/info'),
  mcpHallOfFame: () => fetchJson('/mcp-hall-of-fame'),
  sectionRefs: (act: string, section: string, limit?: number) =>
    fetchJson(`/section-refs/${act}/${section}${limit ? `?limit=${limit}` : ''}`),
  graphRelated: (key: string, limit: number = 5, edgeTypes?: string) =>
    fetchJson(`/graph/related?key=${encodeURIComponent(key)}&limit=${limit}${edgeTypes ? `&edge_types=${encodeURIComponent(edgeTypes)}` : ''}`),
  privateRulingsTree: () => fetchJson('/private-rulings/tree'),
  privateRulingsByYear: (year: number, limit: number = 50, offset: number = 0) =>
    fetchJson(`/private-rulings?year=${year}&limit=${limit}&offset=${offset}`),
  privateRulingsByMonth: (year: number, month: number, limit: number = 200, offset: number = 0) =>
    fetchJson(`/private-rulings?year=${year}&month=${month}&limit=${limit}&offset=${offset}`),
  privateRulingsUndated: (limit: number = 50, offset: number = 0) =>
    fetchJson(`/private-rulings?undated=true&limit=${limit}&offset=${offset}`),
  privateRuling: (authnum: string) => fetchJson(`/private-ruling/${authnum}`),
  sectionDefinedTerms: (act: string, section: string) => fetchJson(`/section-defined-terms/${act}/${section}`),

  // Treaty endpoints
  treaties: () => fetchJson('/treaties'),
  treatyFullTree: () => fetchJson('/treaties/full-tree'),
  treatyTree: (country: string) => fetchJson(`/treaties/${country}`),
  treatyArticle: (country: string, article: string) => fetchJson(`/treaties/${country}/article/${article}`),
  treatySearch: (q: string, country?: string) => {
    let url = `/treaties/search?q=${encodeURIComponent(q)}`
    if (country) url += `&country=${country}`
    return fetchJson(url)
  },

  // ATO Legal Database (proxied through backend/routes/ato.py)
  atoSearch: (q: string, start: number = 1, pageSize: number = 20, df: string = '') => {
    let url = `/ato/search?q=${encodeURIComponent(q)}&start=${start}&pageSize=${pageSize}`
    if (df) url += `&df=${encodeURIComponent(df)}`
    return fetchJson(url)
  },
}
