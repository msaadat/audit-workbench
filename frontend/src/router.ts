import { createRouter, createWebHistory } from 'vue-router'
import HomeView from './views/HomeView.vue'
import WorkspaceView from './views/WorkspaceView.vue'
import DebugView from './views/DebugView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/workspace/:id', name: 'workspace', component: WorkspaceView, props: true },
    { path: '/workspace/:id/debug', name: 'debug', component: DebugView, props: true },
  ],
})
