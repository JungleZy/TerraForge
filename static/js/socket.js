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
 */
(function () {
    'use strict';

    let instance = null;

    function get() {
        if (instance) return instance;
        if (typeof io !== 'function') {
            // socket.io 库没加载（理论上不该发生 —— 它现在是全局 vendor）。
            // 返回 null 而不是抛：调用方都有 null 守卫，实时推送降级即可，
            // 不该把整个页面脚本打挂。
            console.warn('socket.io 库未加载，实时推送不可用');
            return null;
        }
        instance = io();
        if (window.initConnectionStatus) window.initConnectionStatus(instance);
        return instance;
    }

    window.TerraSocket = { get: get };

    // 立即建立连接：底图解压进度是**启动就开始**的全局事件，等某个页面脚本
    // 想起来调 get() 就晚了。
    get();
})();
