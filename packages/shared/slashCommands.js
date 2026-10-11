/** Shared slash-command catalog and parser for Desktop and the VS Code WebView.
 * Keep the factory self-contained so its source can be embedded under the WebView nonce.
 * Commands emit client actions; only client adapters perform IO.
 */
export function createSlashCommands(context) {
    const BUILTIN_COMMANDS = [
      { name: '/follow-up', desc: '查看/设置跟进方式（queue/steer）', badge: '' },
      { name: '/queue', desc: '查看排队消息 / 引导当前任务', badge: '' },
      { name: '/add', desc: '添加模型或配置组', badge: '' },
      { name: '/del', desc: '删除模型或配置组', badge: '' },
      { name: '/fork', desc: '从指定或最新回复分叉会话', badge: '' },
      { name: '/help',          desc: '显示帮助',                    badge: '' },
      { name: '/plan',          desc: '切换到计划模式（只读分析）',    badge: '' },
      { name: '/agent',         desc: '切换到 Agent 模式 / 查看 Agent', badge: '' },
      { name: '/plan-status',   desc: '显示当前计划状态',             badge: '' },
      { name: '/agents',        desc: '列出托管的 Agent',             badge: '' },
      { name: '/agent-log',     desc: '查看 Agent transcript',         badge: '' },
      { name: '/agent-send',    desc: '向 Agent 追加输入',              badge: '' },
      { name: '/wait',          desc: '等待 Agent 完成',                badge: '' },
      { name: '/cancel-agent',  desc: '取消 Agent',                    badge: '' },
      { name: '/spawn-agent',   desc: '启动后台 Agent（支持类型/名称/模型）', badge: '' },
      { name: '/goal',          desc: '设置或管理持久 Goal',             badge: '' },
      { name: '/tasks',         desc: '列出、查看、停止后台任务',          badge: '' },
      { name: '/peers',         desc: '列出其他 CrabCode 会话',          badge: '' },
      { name: '/peer-send',     desc: '向其他会话发送消息',              badge: '' },
      { name: '/status',        desc: '显示会话状态',                 badge: '' },
      { name: '/effort',        desc: '查看/设置推理强度',             badge: '' },
      { name: '/ultra',         desc: '切换/设置 Ultra mode',         badge: '' },
      { name: '/model',         desc: '显示/切换模型',                badge: '' },
      { name: '/new',           desc: '开始新会话',                   badge: '' },
      { name: '/compact',       desc: '压缩对话上下文',               badge: '' },
      { name: '/clear',         desc: '清除历史记录',                 badge: '' },
      { name: '/sessions',      desc: '列出所有会话',                 badge: '' },
      { name: '/recent',        desc: '列出最近的会话',               badge: '' },
      { name: '/search',        desc: '搜索会话',                     badge: '' },
      { name: '/archive',       desc: '归档会话',                     badge: '' },
      { name: '/prune',         desc: '归档/清理过期会话',             badge: '' },
      { name: '/export',        desc: '导出会话 (md/json，可指定路径)',     badge: '' },
      { name: '/stats',         desc: '使用统计',                     badge: '' },
      { name: '/checkpoint',    desc: '创建检查点（含文件快照）',      badge: '' },
      { name: '/checkpoints',   desc: '列出检查点',                   badge: '' },
      { name: '/rollback',      desc: '回滚对话到检查点',             badge: '' },
      { name: '/revert',        desc: '还原文件和对话到检查点',       badge: '' },
      { name: '/undo',          desc: '撤销最后一个检查点',           badge: '' },
      { name: '/resume',        desc: '恢复会话',                     badge: '' },
      { name: '/logs',          desc: '显示后台日志',                 badge: '' },
      { name: '/team',          desc: '团队管理（创建/协作/任务板）',       badge: '' },
      { name: '/schedule',      desc: '定时任务管理（创建/执行/历史）',       badge: '' },
      { name: '/image',         desc: '附加图片到下一条消息',             badge: '' },
    ];

    const EFFORT_LEVELS = ['auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

    function integer(value) {
      return /^-?[0-9]+$/.test(String(value)) ? Number(value) : NaN;
    }

    function shellTokens(raw) {
      const tokens = [];
      const pattern = /"([^"\\]*(?:\\.[^"\\]*)*)"|'([^']*)'|(\S+)/g;
      let match;
      while ((match = pattern.exec(raw || '')) !== null) {
        tokens.push(match[1] !== undefined ? match[1].replace(/\\"/g, '"') : (match[2] !== undefined ? match[2] : match[3]));
      }
      return tokens;
    }

    function parseSessionLaunchArgs(raw, requireSelector) {
      const tokens = shellTokens(raw);
      const options = {};
      const positionals = [];
      const optionNames = {
        '--model': 'model', '-m': 'model',
        '--provider': 'provider',
        '--base-url': 'base_url',
        '--api-format': 'api_format',
        '--model-profile': 'model_profile', '-M': 'model_profile',
      };
      for (let i = 0; i < tokens.length; i += 1) {
        const token = tokens[i];
        let key = token;
        let value = null;
        const equal = token.indexOf('=');
        if (equal > 0) {
          key = token.slice(0, equal);
          value = token.slice(equal + 1);
        }
        const field = optionNames[key];
        if (field) {
          if (value === null) value = tokens[++i];
          if (!value) return { error: '选项 ' + key + ' 需要一个值。' };
          options[field] = value;
        } else if (token.charAt(0) === '-') {
          return { error: '未知选项：' + token };
        } else {
          positionals.push(token);
        }
      }
      if (requireSelector && positionals.length !== 1) {
        return { error: '用法：/resume <session-id> [--model MODEL] [--provider PROVIDER] [--base-url URL] [--api-format FORMAT] [--model-profile PROFILE]' };
      }
      if (!requireSelector && positionals.length > 0) {
        return { error: '用法：/new [--model MODEL] [--provider PROVIDER] [--base-url URL] [--api-format FORMAT] [--model-profile PROFILE]' };
      }
      return { selector: positionals[0] || null, options: options };
    }

    function openModelSettings(action, args) {
      const kind = args.toLowerCase();
      if (kind && kind !== 'model' && kind !== 'group') context.showMessage('用法：/' + action + ' [model|group]');
      else context.postMessage({ type: 'openModelSettings', action: action, kind: kind || 'model' });
      return true;
    }

    const DIRECT_COMMANDS = {
      '/follow-up': function(args) {
        const mode = args.toLowerCase();
        if (mode && mode !== 'queue' && mode !== 'steer') context.showMessage('用法：/follow-up [queue|steer]');
        else context.postMessage(mode ? { type: 'setFollowUpMode', mode: mode } : { type: 'fetchFollowUpMode' });
        return true;
      },
      '/queue': function(args) {
        const tokens = shellTokens(args);
        if (!tokens.length) context.postMessage({ type: 'fetchQueue' });
        else if (tokens[0] === 'steer' && tokens.length <= 2 && (!tokens[1] || /^[1-9][0-9]*$/.test(tokens[1]))) {
          context.postMessage({ type: 'steerQueue', index: tokens[1] ? Number(tokens[1]) : 1 });
        } else context.showMessage('用法：/queue [steer [序号]]');
        return true;
      },
      '/add': function(args) { return openModelSettings('add', args); },
      '/del': function(args) { return openModelSettings('del', args); },
      '/fork': function(args) {
        const tokens = shellTokens(args);
        if (tokens.length > 1) context.showMessage('用法：/fork [message-uuid]');
        else context.postMessage({ type: 'forkSession', messageUuid: tokens[0] || null });
        return true;
      },
      '/help': function() {
        const bt = String.fromCharCode(96);
        const lines = BUILTIN_COMMANDS.map(function(c) {
          return '- ' + bt + c.name + bt + ' — ' + c.desc;
        });
        if (context.shellCommandHelp) lines.push(context.shellCommandHelp);
        if ((context.skills || []).length > 0) {
          lines.push('');
          lines.push('**Skills**');
          (context.skills || []).forEach(function(s) {
            lines.push('- ' + bt + '/' + s.name + bt + ' — ' + (s.description || ''));
          });
        }
        context.showMessage('## CrabCode 命令\n\n' + lines.join('\n'));
        return true;
      },
      '/model': function(args) {
        if (args) {
          context.postMessage({ type: 'setModel', name: args });
        } else {
          context.postMessage({ type: 'fetchModel' });
        }
        return true;
      },
      '/compact': function(args) {
        context.postMessage({ type: 'compact', customInstructions: args || '' });
        return true;
      },
      '/new': function(args) {
        const parsed = parseSessionLaunchArgs(args, false);
        if (parsed.error) { context.showMessage(parsed.error); return true; }
        context.postMessage({ type: 'newSession', options: parsed.options });
        return true;
      },
      '/clear': function() {
        context.postMessage({ type: 'clearMessages' });
        return true;
      },
      '/status': function() {
        context.postMessage({ type: 'fetchStatus' });
        return true;
      },
      '/effort': function(args) {
        const effort = (args || '').toLowerCase();
        if (effort && EFFORT_LEVELS.indexOf(effort) < 0) {
          context.showMessage('用法：/effort <auto|none|minimal|low|medium|high|xhigh|max>');
          return true;
        }
        context.postMessage({ type: 'setEffort', effort: effort || null });
        return true;
      },
      '/ultra': function(args) {
        const value = (args || '').toLowerCase();
        if (value && value !== 'true' && value !== 'false') {
          context.showMessage('用法：/ultra [true|false]；不带参数时切换');
          return true;
        }
        context.postMessage({
          type: 'setUltra',
          enabled: value ? value === 'true' : null,
        });
        return true;
      },
      '/plan': function() {
        context.postMessage({ type: 'switchMode', mode: 'plan' });
        return true;
      },
      '/agent': function(args) {
        if (args) {
          context.postMessage({ type: 'fetchAgent', agentId: args.split(/\s+/)[0] });
          return true;
        }
        context.postMessage({ type: 'switchMode', mode: 'agent' });
        return true;
      },
      '/sessions': function() {
        context.postMessage({ type: 'fetchSessions' });
        return true;
      },
      '/recent': function(args) {
        const limit = args ? integer(args, 10) : 10;
        if (!Number.isSafeInteger(limit) || limit < 1) context.showMessage('用法：/recent [正整数]');
        else context.postMessage({ type: 'fetchRecentSessions', limit: limit });
        return true;
      },
      '/search': function(args) {
        if (!args) { context.showMessage('用法：/search <关键词>'); return true; }
        context.postMessage({ type: 'searchSessions', query: args });
        return true;
      },
      '/archive': function(args) {
        if (!args) { context.showMessage('用法：/archive <session_id>'); return true; }
        context.postMessage({ type: 'archiveSession', sessionId: args });
        return true;
      },
      '/prune': function(args) {
        const tokens = shellTokens(args);
        let days = 30;
        let deleteFiles = false;
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '--delete-files' || token === '--purge') {
            deleteFiles = true;
          } else if (token === '--days' && tokens[i + 1]) {
            days = integer(tokens[++i], 10);
          } else if (token.indexOf('--days=') === 0) {
            days = integer(token.slice(7), 10);
          } else if (/^\d+$/.test(token)) {
            days = integer(token, 10);
          } else {
            context.showMessage('用法：/prune [days] [--delete-files]');
            return true;
          }
        }
        if (!Number.isSafeInteger(days) || days < 0) {
          context.showMessage('days 必须是非负整数');
          return true;
        }
        context.postMessage({ type: 'pruneSessions', days: days, deleteFiles: deleteFiles });
        return true;
      },
      '/export': function(args) {
        const tokens = shellTokens(args);
        let fmt = 'md';
        let sessionId = null;
        const positional = [];
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '--session' || token === '--id') {
            sessionId = tokens[++i] || null;
            if (!sessionId) { context.showMessage('用法：/export [md|json] [path] [--session ID]'); return true; }
          } else if (token.indexOf('--session=') === 0 || token.indexOf('--id=') === 0) {
            sessionId = token.slice(token.indexOf('=') + 1) || null;
            if (!sessionId) { context.showMessage('用法：/export [md|json] [path] [--session ID]'); return true; }
          } else if (token === 'json') fmt = 'json';
          else if (token === 'md' || token === 'markdown') fmt = 'md';
          else positional.push(token);
        }
        if (positional.length > 1) {
          context.showMessage('用法：/export [md|json] [path] [--session ID]');
          return true;
        }
        context.postMessage({ type: 'exportSession', format: fmt, path: positional[0] || undefined, sessionId: sessionId || undefined });
        return true;
      },
      '/stats': function() {
        context.postMessage({ type: 'fetchStats' });
        return true;
      },
      '/checkpoint': function(args) {
        context.postMessage({ type: 'createCheckpoint', label: args || '' });
        return true;
      },
      '/checkpoints': function() {
        context.postMessage({ type: 'fetchCheckpoints' });
        return true;
      },
      '/rollback': function(args) {
        if (!args) { context.showMessage('用法：/rollback <checkpoint_id>'); return true; }
        context.postMessage({ type: 'rollbackCheckpoint', checkpointId: args });
        return true;
      },
      '/revert': function(args) {
        if (!args) { context.showMessage('用法：/revert <checkpoint_id>'); return true; }
        context.postMessage({ type: 'revertCheckpoint', checkpointId: args });
        return true;
      },
      '/undo': function() {
        context.postMessage({ type: 'undoCheckpoint' });
        return true;
      },
      '/resume': function(args) {
        if (!args) { context.postMessage({ type: 'fetchSessions' }); return true; }
        const parsed = parseSessionLaunchArgs(args, true);
        if (parsed.error) { context.showMessage(parsed.error); return true; }
        context.postMessage({ type: 'resumeSession', sessionId: parsed.selector, options: parsed.options });
        return true;
      },
      '/agents': function() {
        context.postMessage({ type: 'fetchAgents' });
        return true;
      },
      '/agent-log': function(args) {
        const tokens = shellTokens(args);
        if (!tokens[0]) { context.showMessage('用法：/agent-log <agent-id> [lines]'); return true; }
        context.postMessage({ type: 'fetchAgentLog', agentId: tokens[0], lines: integer(tokens[1] || '200', 10) || 200 });
        return true;
      },
      '/agent-send': function(args) {
        const tokens = shellTokens(args);
        let interrupt = false;
        for (let i = tokens.length - 1; i >= 0; i -= 1) {
          if (tokens[i] === '--interrupt') { interrupt = true; tokens.splice(i, 1); }
        }
        if (tokens.length < 2) { context.showMessage('用法：/agent-send <agent-id> [--interrupt] <prompt>'); return true; }
        context.postMessage({ type: 'sendAgentInput', agentId: tokens[0], prompt: tokens.slice(1).join(' '), interrupt: interrupt });
        return true;
      },
      '/wait': function(args) {
        const tokens = shellTokens(args);
        if (!tokens[0]) { context.showMessage('用法：/wait <agent-id> [agent-id ...] [--timeout MS]'); return true; }
        let timeout = null;
        const ids = [];
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '--timeout') {
            timeout = integer(tokens[++i] || '', 10);
          } else if (token.indexOf('--timeout=') === 0) {
            timeout = integer(token.slice(10), 10);
          } else {
            ids.push.apply(ids, token.split(',').filter(Boolean));
          }
        }
        // Preserve the historical /wait <id> <timeout-ms> form.
        if (ids.length === 2 && timeout === null && /^[0-9]+$/.test(ids[1])) {
          timeout = integer(ids.pop(), 10);
        }
        if (!ids.length || (timeout !== null && (!Number.isSafeInteger(timeout) || timeout < 0))) {
          context.showMessage('用法：/wait <agent-id> [agent-id ...] [--timeout MS]');
          return true;
        }
        context.postMessage({ type: 'waitAgent', agentIds: ids, agentId: ids.length === 1 ? ids[0] : undefined, timeoutMs: timeout });
        return true;
      },
      '/cancel-agent': function(args) {
        if (!args) { context.showMessage('用法：/cancel-agent <agent-id>'); return true; }
        context.postMessage({ type: 'cancelAgent', agentId: args.split(/\s+/)[0] });
        return true;
      },
      '/spawn-agent': function(args) {
        const tokens = shellTokens(args);
        let subagentType = 'generalPurpose';
        let name = null;
        let modelProfile = null;
        let callback = true;
        const prompt = [];
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          let key = token;
          let value = null;
          const equal = token.indexOf('=');
          if (equal > 0) {
            key = token.slice(0, equal);
            value = token.slice(equal + 1);
          }
          if (key === '--type' || key === '--subagent-type') {
            value = value === null ? tokens[++i] : value;
            if (!value) { context.showMessage('用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
            subagentType = value;
          } else if (key === '--name') {
            value = value === null ? tokens[++i] : value;
            if (!value) { context.showMessage('用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
            name = value;
          } else if (key === '--model' || key === '--model-profile') {
            value = value === null ? tokens[++i] : value;
            if (!value) { context.showMessage('用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
            modelProfile = value;
          } else if (key === '--callback') {
            value = value === null ? tokens[++i] : value;
            if (value !== 'true' && value !== 'false') { context.showMessage('callback 必须是 true 或 false'); return true; }
            callback = value === 'true';
          } else if (token === '--no-callback') {
            callback = false;
          } else if (token.charAt(0) === '-') {
            context.showMessage('未知选项：' + token + '。用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] <prompt>');
            return true;
          } else {
            prompt.push(token);
          }
        }
        if (!prompt.length) { context.showMessage('用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] [--callback true|false] <prompt>'); return true; }
        context.postMessage({ type: 'spawnAgent', prompt: prompt.join(' '), subagentType: subagentType, name: name, modelProfile: modelProfile, callback: callback });
        return true;
      },
      '/goal': function(args) {
        const tokens = shellTokens(args);
        if (!tokens.length || ['show', 'status', 'view'].indexOf(tokens[0].toLowerCase()) >= 0) {
          context.postMessage({ type: 'fetchGoal' });
          return true;
        }
        let action = tokens[0].toLowerCase();
        if (['pause', 'resume', 'complete', 'blocked', 'clear'].indexOf(action) >= 0) {
          if (tokens.length !== 1) context.showMessage('用法：/goal ' + action);
          else context.postMessage({ type: 'manageGoal', action: action === 'pause' ? 'pause' : action, objective: null, budgetWasSet: false });
          return true;
        }
        if (action !== 'set' && action !== 'edit') {
          // Preserve the original casing of a bare objective.
          action = 'set';
        } else {
          tokens.shift();
        }
        let budgetWasSet = false;
        let tokenBudget = null;
        const objective = [];
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '--no-budget') { budgetWasSet = true; tokenBudget = null; continue; }
          if (token === '--budget') {
            if (!tokens[i + 1]) { context.showMessage('用法：/goal [set|edit] [--budget N|--no-budget] <objective>'); return true; }
            budgetWasSet = true;
            tokenBudget = integer(tokens[++i], 10);
            continue;
          }
          if (token.indexOf('--budget=') === 0) {
            budgetWasSet = true;
            tokenBudget = integer(token.slice(9), 10);
            continue;
          }
          objective.push(token);
        }
        if (!objective.length || (budgetWasSet && (!Number.isSafeInteger(tokenBudget) || tokenBudget <= 0) && tokenBudget !== null)) {
          context.showMessage('用法：/goal [set|edit] [--budget N|--no-budget] <objective>');
          return true;
        }
        context.postMessage({ type: 'manageGoal', action: action, objective: objective.join(' '), tokenBudget: tokenBudget, budgetWasSet: budgetWasSet });
        return true;
      },
      '/tasks': function(args) {
        const tokens = shellTokens(args);
        const sub = (tokens.shift() || 'list').toLowerCase();
        if (sub === 'list') {
          context.postMessage({ type: 'fetchTasks' });
        } else if (sub === 'show' || sub === 'get') {
          if (!tokens[0]) context.showMessage('用法：/tasks show <task-id>');
          else context.postMessage({ type: 'fetchTask', taskId: tokens[0] });
        } else if (sub === 'output' || sub === 'log') {
          if (!tokens[0]) context.showMessage('用法：/tasks output <task-id> [lines]');
          else {
            const outputLines = tokens[1] ? integer(tokens[1], 10) : 200;
            if (!Number.isSafeInteger(outputLines) || outputLines < 1) context.showMessage('lines 必须是正整数');
            else context.postMessage({ type: 'fetchTaskOutput', taskId: tokens[0], lines: outputLines });
          }
        } else if (sub === 'stop') {
          if (!tokens[0]) context.showMessage('用法：/tasks stop <task-id>');
          else context.postMessage({ type: 'stopTask', taskId: tokens[0] });
        } else {
          context.showMessage('用法：/tasks [list|show|output|stop] <task-id>');
        }
        return true;
      },
      '/peers': function() {
        context.postMessage({ type: 'fetchPeers' });
        return true;
      },
      '/peer-send': function(args) {
        const tokens = shellTokens(args);
        if (tokens.length < 2) { context.showMessage('用法：/peer-send <session|name> <text>'); return true; }
        context.postMessage({ type: 'sendPeerMessage', to: tokens[0], text: tokens.slice(1).join(' ') });
        return true;
      },
      '/team': function(args) {
        const tokens = shellTokens(args);
        const sub = (tokens.shift() || 'list').toLowerCase();
        if (sub === 'list') { context.postMessage({ type: 'fetchTeams' }); return true; }
        if (sub === 'create') {
          const name = tokens.shift();
          if (!name) { context.showMessage('用法：/team create <name> [max-teammates]'); return true; }
          const max = tokens.shift();
          const maxTeammates = max ? integer(max, 10) : null;
          if (max && (!Number.isSafeInteger(maxTeammates) || maxTeammates < 1)) { context.showMessage('max-teammates 必须是正整数'); return true; }
          context.postMessage({ type: 'createTeam', name: name, maxTeammates: maxTeammates });
          return true;
        }
        const teamId = tokens.shift();
        if (!teamId) { context.showMessage('用法：/team [list|create|status|messages|tasks|spawn|remove|message|broadcast|mark-read|task-add|task-claim|task-complete|task-fail|bridge|bridge-status|cross-message|shutdown] <team-id>'); return true; }
        if (sub === 'status') {
          context.postMessage({ type: 'fetchTeamStatus', teamId: teamId });
        } else if (sub === 'messages') {
          let agentId = null;
          let unread = false;
          for (let i = 0; i < tokens.length; i += 1) {
            if (tokens[i] === '--unread') unread = true;
            else if (tokens[i] === '--agent' || tokens[i] === '--agent-id') agentId = tokens[++i] || null;
            else if (tokens[i].indexOf('--agent=') === 0) agentId = tokens[i].slice(8);
            else { context.showMessage('用法：/team messages <team-id> [--agent ID] [--unread]'); return true; }
          }
          context.postMessage({ type: 'fetchTeamMessages', teamId: teamId, agentId: agentId, unread: unread });
        } else if (sub === 'tasks') {
          context.postMessage({ type: 'fetchTeamTasks', teamId: teamId });
        } else if (sub === 'remove') {
          const agentId = tokens.shift();
          if (!agentId || tokens.length) context.showMessage('用法：/team remove <team-id> <agent-id>');
          else context.postMessage({ type: 'removeTeamMember', teamId: teamId, agentId: agentId });
        } else if (sub === 'shutdown') {
          context.postMessage({ type: 'shutdownTeam', teamId: teamId });
        } else if (sub === 'spawn') {
          let role = 'worker';
          let name = null;
          let modelProfile = null;
          const prompt = [];
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            let key = token;
            let value = null;
            const equal = token.indexOf('=');
            if (equal > 0) { key = token.slice(0, equal); value = token.slice(equal + 1); }
            if (key === '--role') {
              value = value === null ? tokens[++i] : value;
              if (!value) { context.showMessage('用法：/team spawn <team-id> [--role ROLE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
              role = value;
            } else if (key === '--name') {
              value = value === null ? tokens[++i] : value;
              if (!value) { context.showMessage('用法：/team spawn <team-id> [--role ROLE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
              name = value;
            } else if (key === '--model' || key === '--model-profile') {
              value = value === null ? tokens[++i] : value;
              if (!value) { context.showMessage('用法：/team spawn <team-id> [--role ROLE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
              modelProfile = value;
            } else if (token.charAt(0) === '-') {
              context.showMessage('未知选项：' + token); return true;
            } else {
              prompt.push(token);
            }
          }
          if (!prompt.length) { context.showMessage('用法：/team spawn <team-id> [--role ROLE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
          if (['lead', 'worker', 'researcher', 'reviewer'].indexOf(role) < 0) { context.showMessage('role 必须是 lead、worker、researcher 或 reviewer'); return true; }
          context.postMessage({ type: 'spawnTeamMember', teamId: teamId, prompt: prompt.join(' '), role: role, name: name, modelProfile: modelProfile });
        } else if (sub === 'message') {
          const to = tokens.shift();
          const text = tokens.join(' ');
          if (!to || !text) context.showMessage('用法：/team message <team-id> <agent-id> <text>');
          else context.postMessage({ type: 'sendTeamMessage', teamId: teamId, to: to, text: text });
        } else if (sub === 'broadcast') {
          const text = tokens.join(' ');
          if (!text) context.showMessage('用法：/team broadcast <team-id> <text>');
          else context.postMessage({ type: 'broadcastTeamMessage', teamId: teamId, text: text });
        } else if (sub === 'mark-read') {
          const agentId = tokens.shift();
          if (!agentId) context.showMessage('用法：/team mark-read <team-id> <agent-id> [message-id ...]');
          else context.postMessage({ type: 'markTeamMessagesRead', teamId: teamId, agentId: agentId, messageIds: tokens.length ? tokens : undefined });
        } else if (sub === 'task-add') {
          const description = tokens.join(' ');
          if (!description) context.showMessage('用法：/team task-add <team-id> <description>');
          else context.postMessage({ type: 'addTeamTask', teamId: teamId, description: description });
        } else if (sub === 'task-claim') {
          const taskId = tokens.shift();
          if (!taskId || tokens.length > 1) context.showMessage('用法：/team task-claim <team-id> <task-id> [agent-id]');
          else context.postMessage({ type: 'claimTeamTask', teamId: teamId, taskId: taskId, agentId: tokens[0] || '' });
        } else if (sub === 'task-complete') {
          const taskId = tokens.shift();
          let agentId = '';
          if (tokens[0] === '--agent' || tokens[0] === '--agent-id') {
            tokens.shift();
            agentId = tokens.shift() || '';
          }
          const result = tokens.join(' ');
          if (!taskId) context.showMessage('用法：/team task-complete <team-id> <task-id> [--agent ID] [result]');
          else context.postMessage({ type: 'completeTeamTask', teamId: teamId, taskId: taskId, result: result, agentId: agentId });
        } else if (sub === 'task-fail') {
          const taskId = tokens.shift();
          let agentId = '';
          if (tokens[0] === '--agent' || tokens[0] === '--agent-id') {
            tokens.shift();
            agentId = tokens.shift() || '';
          }
          const reason = tokens.join(' ');
          if (!taskId) context.showMessage('用法：/team task-fail <team-id> <task-id> [--agent ID] [reason]');
          else context.postMessage({ type: 'failTeamTask', teamId: teamId, taskId: taskId, reason: reason, agentId: agentId });
        } else if (sub === 'bridge' || sub === 'bridge-status') {
          const otherTeam = tokens.shift();
          if (!otherTeam || (sub === 'bridge' && tokens.length > 1)) {
            context.showMessage('用法：/team bridge <team-a> <team-b> [allow_all|allow_tagged|deny]');
          } else if (sub === 'bridge-status' && tokens.length) {
            context.showMessage('用法：/team bridge-status <team-a> <team-b>');
          } else if (sub === 'bridge') {
            const policy = tokens[0] || 'allow_tagged';
            context.postMessage({ type: 'registerTeamBridge', teamA: teamId, teamB: otherTeam, policy: policy });
          } else {
            context.postMessage({ type: 'getTeamBridge', teamA: teamId, teamB: otherTeam });
          }
        } else if (sub === 'cross-message') {
          const toTeam = tokens.shift();
          let fromAgent = '';
          let toAgent = '';
          const textParts = [];
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            if (token === '--from-agent' || token === '--from') fromAgent = tokens[++i] || '';
            else if (token === '--to-agent' || token === '--to') toAgent = tokens[++i] || '';
            else textParts.push(token);
          }
          if (!toTeam || !textParts.length) context.showMessage('用法：/team cross-message <from-team> <to-team> [--from-agent ID] [--to-agent ID] <text>');
          else context.postMessage({ type: 'sendCrossTeamMessage', fromTeam: teamId, toTeam: toTeam, fromAgent: fromAgent, toAgent: toAgent, text: textParts.join(' ') });
        } else {
          context.showMessage('用法：/team [list|create|status|messages|tasks|spawn|remove|message|broadcast|mark-read|task-add|task-claim|task-complete|task-fail|bridge|bridge-status|cross-message|shutdown] <team-id>');
        }
        return true;
      },
      '/schedule': function(args) {
        const tokens = shellTokens(args);
        const sub = (tokens.shift() || 'list').toLowerCase();
        if (sub === 'list') {
          let status = null;
          let scheduleType = null;
          let enabled = null;
          let limit = 100;
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            let key = token;
            let value = null;
            const equal = token.indexOf('=');
            if (equal > 0) { key = token.slice(0, equal); value = token.slice(equal + 1); }
            if (key === '--status') { value = value === null ? tokens[++i] : value; status = value || null; }
            else if (key === '--type' || key === '--schedule-type') { value = value === null ? tokens[++i] : value; scheduleType = value || null; }
            else if (key === '--enabled') {
              value = value === null ? tokens[++i] : value;
              if (value !== 'true' && value !== 'false') { context.showMessage('enabled 必须是 true 或 false'); return true; }
              enabled = value === 'true';
            } else if (key === '--limit') {
              value = value === null ? tokens[++i] : value;
              limit = integer(value || '', 10);
              if (!Number.isSafeInteger(limit) || limit < 1) { context.showMessage('limit 必须是正整数'); return true; }
            } else {
              context.showMessage('用法：/schedule list [--status STATUS] [--type cron|interval|once] [--enabled true|false] [--limit N]');
              return true;
            }
            if (value === null || value === '') { context.showMessage('选项 ' + key + ' 需要一个值'); return true; }
          }
          context.postMessage({ type: 'fetchSchedules', status: status || undefined, scheduleType: scheduleType || undefined, enabled: enabled === null ? undefined : enabled, limit: limit });
          return true;
        }
        if (sub === 'show' || sub === 'status') {
          if (tokens.length !== 1) { context.showMessage('用法：/schedule show <job-id>'); return true; }
          context.postMessage({ type: 'fetchSchedule', jobId: tokens[0] });
          return true;
        }
        if (sub === 'runs' || sub === 'history') {
          const jobId = tokens.shift();
          if (!jobId) { context.showMessage('用法：/schedule runs <job-id> [--status STATUS] [--limit N]'); return true; }
          let status = null;
          let limit = 50;
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            let key = token;
            let value = null;
            const equal = token.indexOf('=');
            if (equal > 0) { key = token.slice(0, equal); value = token.slice(equal + 1); }
            if (key === '--status') { value = value === null ? tokens[++i] : value; status = value || null; }
            else if (key === '--limit') {
              value = value === null ? tokens[++i] : value;
              limit = integer(value || '', 10);
              if (!Number.isSafeInteger(limit) || limit < 1) { context.showMessage('limit 必须是正整数'); return true; }
            } else { context.showMessage('用法：/schedule runs <job-id> [--status STATUS] [--limit N]'); return true; }
            if (value === null || value === '') { context.showMessage('选项 ' + key + ' 需要一个值'); return true; }
          }
          context.postMessage({ type: 'fetchScheduleRuns', jobId: jobId, status: status || undefined, limit: limit });
          return true;
        }
        if (sub === 'pause' || sub === 'resume' || sub === 'run' || sub === 'cancel') {
          if (tokens.length !== 1) { context.showMessage('用法：/schedule ' + sub + ' <job-id>'); return true; }
          context.postMessage({ type: 'mutateSchedule', action: sub, jobId: tokens[0] });
          return true;
        }
        if (sub === 'create') {
          const positionals = [];
          const request = { tags: [], enabled: true, extra: {} };
          let promptMode = false;
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            if (token === '--') { promptMode = true; continue; }
            if (promptMode) { positionals.push(token); continue; }
            let key = token;
            let value = null;
            const equal = token.indexOf('=');
            if (equal > 0) { key = token.slice(0, equal); value = token.slice(equal + 1); }
            if (key === '--disabled') {
              if (value !== null) { context.showMessage('--disabled 不接受值'); return true; }
              request.enabled = false;
            } else if (key === '--max-runs' || key === '--timeout' || key === '--model' || key === '--model-profile' || key === '--cwd' || key === '--description' || key === '--tag' || key === '--enabled' || key === '--next-run' || key === '--job-session' || key === '--run-session' || key === '--extra') {
              value = value === null ? tokens[++i] : value;
              if (!value) { context.showMessage('选项 ' + key + ' 需要一个值'); return true; }
              if (key === '--max-runs' || key === '--timeout') {
                const numeric = integer(value, 10);
                if (!Number.isSafeInteger(numeric) || numeric < 1) { context.showMessage(key + ' 必须是正整数'); return true; }
                request[key === '--max-runs' ? 'max_runs' : 'timeout'] = numeric;
              } else if (key === '--enabled') {
                if (value !== 'true' && value !== 'false') { context.showMessage('enabled 必须是 true 或 false'); return true; }
                request.enabled = value === 'true';
              } else if (key === '--next-run') request.next_run = value;
              else if (key === '--job-session' || key === '--run-session') request.job_session_id = value;
              else if (key === '--extra') {
                try {
                  const parsed = JSON.parse(value);
                  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('object required');
                  Object.assign(request.extra, parsed);
                } catch (_) {
                  context.showMessage('--extra 必须是 JSON 对象');
                  return true;
                }
              } else if (key === '--model' || key === '--model-profile') request.model_profile = value;
              else if (key === '--cwd') request.cwd = value;
              else if (key === '--description') request.description = value;
              else request.tags.push(value);
            } else if (token.charAt(0) === '-' && token !== '-') {
              context.showMessage('未知选项：' + token); return true;
            } else positionals.push(token);
          }
          if (positionals.length < 4) {
            context.showMessage('用法：/schedule create [选项] <name> <cron|interval|once> <schedule> <prompt>');
            return true;
          }
          const kind = positionals[1];
          if (kind !== 'cron' && kind !== 'interval' && kind !== 'once') {
            context.showMessage('schedule_type 必须是 cron、interval 或 once'); return true;
          }
          request.name = positionals[0];
          request.schedule_type = kind;
          request.schedule = positionals[2];
          request.prompt = positionals.slice(3).join(' ');
          context.postMessage({ type: 'createSchedule', request: request });
          return true;
        }
        context.showMessage('用法：/schedule [list|show|runs|create|pause|resume|run|cancel] ...');
        return true;
      },
      '/plan-status': function() {
        context.postMessage({ type: 'fetchPlanStatus' });
        return true;
      },
      '/logs': function(args) {
        const tokens = shellTokens(args);
        let tail = 100;
        let name = null;
        let clear = false;
        let follow = false;
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '-f' || token === '--follow') { follow = true; continue; }
          if (token === '--stop') { context.postMessage({ type: 'stopLogFollow' }); return true; }
          if (token === '--clear') { clear = true; continue; }
          if (token === '--tail') { tail = integer(tokens[++i], 10); continue; }
          if (token.indexOf('--tail=') === 0) { tail = integer(token.slice(7), 10); continue; }
          if (token.charAt(0) !== '-' && !name) name = token;
          else { context.showMessage('用法：/logs [-f|--clear] [--tail N] [name]'); return true; }
        }
        if (!Number.isSafeInteger(tail) || tail < 1 || (follow && clear)) { context.showMessage('用法：/logs [-f|--clear] [--tail N] [name]'); return true; }
        if (follow) {
          if (!name) context.showMessage('用法：/logs -f <name>');
          else context.postMessage({ type: 'followLogs', name: name });
        } else {
          if (clear && !name) { context.showMessage('用法：/logs --clear <name>'); return true; }
          context.postMessage({ type: 'fetchLogs', lines: tail, tail: tail, name: name, clear: clear });
        }
        return true;
      },
      '/image': function(args) {
        const paths = shellTokens(args);
        if (!paths.length) { context.showMessage('用法：/image <path> [path2 ...]'); return true; }
        context.postMessage({ type: 'attachImagePaths', paths: paths });
        return true;
      },
    };

    // Commands without arguments must never silently discard a typo.
    ['/help', '/plan', '/clear', '/status', '/sessions', '/stats', '/checkpoints', '/undo', '/agents', '/peers', '/plan-status'].forEach(function(name) {
      const handler = DIRECT_COMMANDS[name];
      DIRECT_COMMANDS[name] = function(args) {
        if (args.trim()) context.showMessage('用法：' + name);
        else handler(args);
        return true;
      };
    });
    return { commands: BUILTIN_COMMANDS, handlers: DIRECT_COMMANDS };
}
