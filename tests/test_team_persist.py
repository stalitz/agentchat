# 回归测试（后端 store）：team_id 必须随发起/续跑选择落库，防静默降级成 2 agent。
#
# 根因：原 create_session 在 handle_start 创建时未传 team_id，且后端无任何把运行时 Team 写回
# 会话的路径——stored team_id 停留在创建默认 teams[0]。实测五子棋会话首跑 3g 但 stored=2g，
# 重开恢复 2g 续跑无裁判。
#
# 本测试锁住：update_session_team 能把 team_id 写回并持久化到磁盘（handle_start 每次发起调用它）。
import os, sys, json, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from backend import store


def main():
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, 'proj')
    os.makedirs(proj)

    s = store.create_session(proj, 't', 'tm_2g')
    assert s['team_id'] == 'tm_2g', s['team_id']

    # 模拟 handle_start 把运行时选择的 3g 写回
    store.update_session_team(proj, s['id'], 'tm_3g')

    # 内存读回
    back = store.read_session(proj, s['id'])
    assert back['team_id'] == 'tm_3g', back['team_id']

    # 磁盘持久化读回
    data = json.load(open(os.path.join(proj, '.agentchat', 'sessions.json')))
    sess = data if isinstance(data, list) else data.get('sessions', [])
    items = sess if isinstance(sess, list) else list(sess.values())
    found = [x for x in items if x['id'] == s['id']][0]
    assert found['team_id'] == 'tm_3g', found['team_id']

    print('PASS: update_session_team 把 team_id 写回并持久化到磁盘')
    print('team_id 持久化回归测试通过 ✓')


if __name__ == '__main__':
    main()
