import * as THREE from 'three'
import nodeVert from './node.vert.glsl'
import nodeFrag from './node.frag.glsl'
import edgeVert from './edge.vert.glsl'
import edgeFrag from './edge.frag.glsl'

export const getNodeMaterial = () => {
  return new THREE.ShaderMaterial({
    // TODO Try to make it FrontSide
    // Works for rendering, but not for the raycaster
    side: THREE.DoubleSide,
    vertexShader: nodeVert,
    fragmentShader: nodeFrag,
    uniforms: {
      uHasOutline: { value: 0.0 },
      uOpacity: { value: 1.0 },
      uShape: { value: 0.0 },
    },
    blending: THREE.CustomBlending,
    blendSrc: THREE.SrcAlphaFactor,
    depthTest: false,
  })
}

export const getEdgeMaterial = () =>
  new THREE.ShaderMaterial({
    // TODO Try to make it FrontSide
    // Works for rendering, but not for the raycaster
    // Maybe due to clockwise front detection
    side: THREE.DoubleSide,
    vertexShader: edgeVert,
    fragmentShader: edgeFrag,
    uniforms: {
      uOpacity: { value: 1.0 },
      // Flowing-energy animation (Grid Studio). uFlowStrength defaults to 0 so
      // ordinary graphs render exactly as before; the grid panel raises it.
      uFlowTime: { value: 0.0 },
      uFlowStrength: { value: 0.0 },
      uFlowSpeed: { value: 0.5 },
      uFlowDensity: { value: 3.0 },
      // per-instance (set via setUniformAt): pulse speed multiplier for this
      // edge. 0 = no flow (a dead / tripped line stops moving entirely).
      uFlowRate: { value: 1.0 },
      // per-instance draw progress [0..1] for a grow-from-source reveal. 1 =
      // fully drawn (default, so ordinary graphs are unaffected).
      uDraw: { value: 1.0 },
    },
    blending: THREE.CustomBlending,
    blendSrc: THREE.SrcAlphaFactor,
    depthTest: false,
  })

export const getBoxSelectionMaterial = () =>
  new THREE.MeshBasicMaterial({
    side: THREE.FrontSide,
    blending: THREE.CustomBlending,
    blendSrc: THREE.SrcAlphaFactor,
    depthTest: false,
    color: 0x2255ff,
    opacity: 0.3,
  })
