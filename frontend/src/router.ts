import { createRouter, createWebHistory } from 'vue-router'
import HomeView from './views/HomeView.vue'
import WorkspaceView from './views/WorkspaceView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/workspace/:id', name: 'workspace', component: WorkspaceView, props: true },
  ],
})
