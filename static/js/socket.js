/**
 * 全局 Socket.IO 单例。
 *
 * 为什么要有它：socket 实例原本由 tasks.js 创建（`socket = io()`），只有首页有。
 * 底图解压进度要在**所有页面**显示，而 /history、/config 根本没有连接。把创建点
 * 提到这里，各页按需 get()，全站只有一个连接。
 *
 * ⚠️ 不能写成 `window.socket = io()`：tasks.js 顶部的 `let socket` 在全局作用域会
 * **遮蔽** window.socket，两边看到的是不同的东西（`let` 声明不挂到 window 上，
 * 但同作用域内的引用会解析到它）。用带命名空间的 window.TerraSocket 避开。
 *
 * ⚠️⚠️ **调用方必须在 get() 之后的同一个同步块里立刻注册监听**（`socket.on(...)`），
 * 中间不许插入 await / setTimeout / 动态 import，更不许把注册留到下一个 <script>。
 * 原因：socket.io **不重放**已经错过的事件。第一次 get() 才真正 io() 建连，握手是
 * 异步的，但 get() 内部 io() 之后是同步返回的 —— 中间没有让出事件循环的机会，所以
 * 第一个消费者在同一个同步块里注册就是零窗口，握手响应最早也只能等本块跑完才派发。
 *
 * 反例（本文件初版就是这么写的）：在解析期先 get() 建好连接，把 socket.on 留给后面
 * 的 <script src> 取回后再注册 —— 经典脚本取回期间事件循环照常派发网络回调，connect
 * 完全可能先到。错过 connect 的后果不是「晚一点收到」而是**永远收不到**：
 *   - tasks.js 的 hasConnectedOnce 永远停在 false，断线重连后不补拉数据；
 *   - 服务端只在 connect 时推一次底图状态快照，而解压失败是终态、之后没有增量事件、
 *     也没有 REST 端点能补拿，错过了就永远不显示失败。
 *
 * 库没加载时 get() 返回 null。注意：**现有调用方并没有判空**（tasks.js 拿到就直接
 * .on()），这在今天是安全的 —— socket.io 已是全局 vendor，它都加载不出来的话整页
 * 脚本本来就废了。新增消费者请自己判空：
 * `const s = window.TerraSocket.get(); if (!s) return;`
 */
(function () {
    'use strict';

    let instance = null;

    function get() {
        if (instance) return instance;
        if (typeof io !== 'function') {
            // socket.io 库没加载（理论上不该发生 —— 它现在是全局 vendor）。
            // 返回 null 而不是抛：实时推送降级即可，不该把整个页面脚本打挂。
            console.warn('socket.io 库未加载，实时推送不可用');
            return null;
        }
        instance = io();
        if (window.initConnectionStatus) window.initConnectionStatus(instance);
        return instance;
    }

    window.TerraSocket = { get: get };

    // 这里**刻意不建立连接**：解析期建连会早于所有消费者注册监听（见上面的 ⚠️⚠️），
    // 连接改由第一个消费者按需触发。
})();
