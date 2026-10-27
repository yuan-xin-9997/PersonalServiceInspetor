const AiAccess={
  template:`<section class="panel ai-access">
    <h2>AI 接入</h2><p>管理自己的自动化凭据。权限始终受所属用户当前权限约束。时间均为北京时间。</p>
    <p v-if="error" role="alert" class="ai-error">{{error}} <button @click="load">重试</button></p>
    <p v-if="loading" role="status">正在加载…</p>
    <template v-if="info">
      <h3>创建凭据</h3><form @submit.prevent="create" class="form-grid">
        <label>名称<input v-model.trim="name" required maxlength="100" placeholder="例如：我的巡检助手"></label>
        <label>有效期（天）<input v-model.number="days" type="number" min="1" max="366" required></label>
        <fieldset><legend>权限范围（默认仅总体状态查询）</legend>
          <label v-for="scope in info.scopes" :key="scope.id" class="ai-scope"><input type="checkbox" :value="scope.id" v-model="scopes">{{scope.description}} <code>{{scope.id}}</code></label>
          <p v-if="!info.scopes.length">当前没有可授权的业务权限，请联系管理员。</p>
        </fieldset>
        <div><button class="primary" :disabled="busy||!scopes.length||!!secret">{{busy?'处理中…':'创建凭据'}}</button></div>
      </form>
      <h3>我的凭据</h3><div class="ai-table"><table>
        <thead><tr><th>名称 / 前缀</th><th>权限</th><th>创建 / 过期（北京时间）</th><th>状态</th><th>操作</th></tr></thead>
        <tbody><tr v-for="row in rows" :key="row.id"><td>{{row.name}}<br><code>{{row.prefix}}</code></td><td>{{row.scopes.join(', ')}}</td><td>{{time(row.created_at)}}<br>{{time(row.expires_at)}}</td><td>{{status(row)}}</td><td><button :disabled="busy||row.status==='revoked'" class="secondary danger" @click="revoke(row)">撤销</button></td></tr>
        <tr v-if="!rows.length&&!loading"><td colspan="5">暂无凭据。创建后完整密钥仅显示一次。</td></tr></tbody>
      </table></div>
      <div class="actions"><button :disabled="!offset||busy" @click="offset-=20;loadList()">上一页</button><span>共 {{total}} 条 · 第 {{Math.floor(offset/20)+1}} 页</span><button :disabled="offset+20>=total||busy" @click="offset+=20;loadList()">下一页</button></div>
      <h3>地址与鉴权</h3><p>API：<a :href="info.api_url+'/summary'">{{info.api_url}}</a><br>OpenAPI：<a :href="info.openapi_url">{{info.openapi_url}}</a><br>交互文档：<a :href="info.docs_url" target="_blank">{{info.docs_url}}</a><br>MCP：<code>{{info.mcp_url}}</code></p>
      <p>API 使用独立 Bearer API Key。MCP 使用 15 分钟短期令牌，普通 API Key 不能直接作为 MCP 令牌。程序以环境变量注入 API Key，自动换取令牌；OAuth 客户端通过浏览器授权、S256 PKCE 与 refresh_token 自动续取。</p>
      <p>内网 HTTPS 使用内部 CA，请先安装管理员提供的 CA 证书。不能访问内网或仅接受公网受信 HTTPS 的云端客户端暂不支持。已验证客户端及完整示例见项目 AI 接入说明。</p>
      <pre>{{example}}</pre><p>工具：get_summary、list_checks、list_incidents、get_history、list_tasks、get_task、get_task_results、run_inspection、set_check_enabled。写入需要显式授权；单项开关还需 confirm=true。没有开放删除、批量修改、发布或远程命令。</p>
      <h3>无副作用连接测试</h3><p>仅在内存中换取 15 分钟令牌，验证认证、MCP 初始化、工具发现和状态查询。不会执行巡检或修改设置。</p>
      <label>待测试的 API Key<input type="password" autocomplete="off" v-model="testKey" placeholder="仅临时用于本次测试"></label>
      <button :disabled="busy||!testKey" @click="test">测试连接</button><pre v-if="testResult" role="status">{{testResult}}</pre>
    </template>
    <div v-if="secret" class="ai-overlay" role="dialog" aria-modal="true" aria-label="保存一次性密钥">
      <div class="panel ai-secret"><h2>完整密钥仅显示这一次</h2><p>请立即复制并保存到受保护的凭据存储。关闭后无法再次查看；遗失时请撤销旧凭据并重新创建。</p>
      <textarea readonly :value="secret" aria-label="一次性完整密钥"></textarea><p role="status">{{copyMessage}}</p>
      <button class="primary" @click="copy">复制密钥</button> <button @click="closeSecret">我已保存，关闭并清除</button></div>
    </div>
    <div v-if="pendingRevoke" class="ai-overlay" role="dialog" aria-modal="true" aria-label="确认撤销凭据"><div class="panel ai-secret"><h2>确认撤销凭据</h2><p>撤销“{{pendingRevoke.name}}”后，使用此凭据及关联短期令牌的所有程序将立即失去访问能力。</p><button class="secondary danger" :disabled="busy" @click="confirmRevoke">确认撤销并立即失效</button> <button :disabled="busy" @click="pendingRevoke=null">取消</button></div></div>
  </section>`,
  data(){return{info:null,rows:[],total:0,offset:0,name:'',days:30,scopes:[],loading:false,busy:false,error:'',secret:'',copyMessage:'',testKey:'',testResult:'',pendingRevoke:null};},
  computed:{example(){return `# 密钥由环境或受保护的凭据存储注入，不要写进脚本\nexport INSPECTOR_URL='${this.info.api_url.replace('/api/v1','')}'\ncurl --cacert "$INSPECTOR_CA_FILE" -H "Authorization: Bearer $INSPECTOR_API_KEY" "$INSPECTOR_URL/api/v1/summary"\n# 官方 SDK 自动换取短期令牌、连接 MCP：\npython examples/ai_client.py\n# OAuth 发现：${this.info.oauth_metadata_url}`;}},
  methods:{
    time(v){return new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});},
    status(row){return row.status==='revoked'?'已撤销':row.status==='expired'||Date.parse(row.expires_at)<=Date.now()?'已过期':'有效';},
    async json(url,options={}){const r=await fetch(url,{...options,cache:'no-store',headers:{'Content-Type':'application/json',...(url.startsWith('/api/ai/')&&options.method&&options.method!=='GET'?{'X-CSRF-Token':this.info?.csrf||''}:{}),...options.headers}});let body;try{body=await r.json();}catch{throw new Error('服务响应异常，请重试');}if(!r.ok)throw new Error(body.error?.message||body.detail||'请求失败');return body;},
    async load(){this.loading=true;this.error='';try{this.info=await this.json('/api/ai/info');this.scopes=this.scopes.filter(s=>this.info.scopes.some(v=>v.id===s));if(!this.scopes.length&&this.info.scopes.some(v=>v.id==='status:read'))this.scopes=['status:read'];await this.loadList();}catch(e){this.error=e.message;}finally{this.loading=false;}},
    async loadList(){try{const r=await this.json('/api/ai/credentials?offset='+this.offset+'&limit=20');this.rows=r.items;this.total=r.total;}catch(e){this.error=e.message;}},
    async create(){this.busy=true;this.error='';try{const r=await this.json('/api/ai/credentials',{method:'POST',body:JSON.stringify({name:this.name,expires_at:new Date(Date.now()+this.days*86400000).toISOString(),scopes:this.scopes})});this.secret=r.secret;this.copyMessage='尚未复制';this.name='';this.offset=0;await this.loadList();}catch(e){this.error=e.message;await this.load();}finally{this.busy=false;}},
    async copy(){try{if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(this.secret);else{const area=this.$el.querySelector('textarea');area.focus();area.select();if(!document.execCommand('copy'))throw new Error();}this.copyMessage='已复制。请保存后再关闭。';}catch{this.copyMessage='复制失败，请选择上方密钥手动复制后保存。';}},
    closeSecret(){this.secret='';this.copyMessage='';},
    revoke(row){this.pendingRevoke=row;},
    async confirmRevoke(){const row=this.pendingRevoke;this.busy=true;this.error='';try{await this.json('/api/ai/credentials/'+row.id,{method:'DELETE'});row.status='revoked';this.pendingRevoke=null;}catch(e){this.error=e.message;this.pendingRevoke=null;}finally{this.busy=false;}},
    async test(){this.busy=true;this.testResult='';let token='';const key=this.testKey;this.testKey='';try{
      const grant=await this.json(this.info.token_url,{method:'POST',headers:{Authorization:'Bearer '+key}});token=grant.access_token;this.testResult='认证：成功（短期令牌最多 15 分钟）';
      const rpc=async(method,params={},version)=>{const r=await fetch(this.info.mcp_url,{method:'POST',cache:'no-store',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',Accept:'application/json, text/event-stream',...(version?{'MCP-Protocol-Version':version}:{})},body:JSON.stringify({jsonrpc:'2.0',id:1,method,params})});const b=await r.json();if(!r.ok||b.error)throw new Error('MCP '+method+' 失败（'+r.status+'）');return b.result;};
      const init=await rpc('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'inspector-browser-test',version:'1'}});this.testResult+='\n初始化：成功（'+init.protocolVersion+'）';
      const tools=await rpc('tools/list',{},init.protocolVersion);this.testResult+='\n工具发现：'+tools.tools.map(t=>t.name).join(', ');
      const result=await rpc('tools/call',{name:'get_summary',arguments:{}},init.protocolVersion);if(result.isError)throw new Error('状态查询失败：请确认 status:read 及所属用户当前权限');this.testResult+='\n业务查询：成功';
    }catch(e){this.testResult+='\n'+e.message;}finally{token='';this.busy=false;}},
    guard(e){if(this.secret){e.preventDefault();e.returnValue='';}}
  },
  mounted(){this.load();this._guard=e=>this.guard(e);window.addEventListener('beforeunload',this._guard);},
  beforeUnmount(){window.removeEventListener('beforeunload',this._guard);this.secret='';this.testKey='';}
};
