#!/usr/bin/env python3
import os
import sys
import pickle
import pytao
import numpy as np
import asyncio
import zmq
import time
from p4p.nt import NTTable
from p4p.server import Server as PVAServer
from p4p.server.asyncio import SharedPV
from zmq.asyncio import Context
import simulacrum


#set up python logger
L = simulacrum.util.SimulacrumLog(os.path.splitext(os.path.basename(__file__))[0], level='INFO')

class ModelService:
    def __init__(self):
        tao_lib = os.environ.get('TAO_LIB', '')
        self.tao = pytao.Tao(so_lib=tao_lib)
        path_to_lattice = os.path.join(os.path.dirname(os.path.realpath(__file__)), "lcls.lat")
        path_to_init = os.path.join(os.path.dirname(os.path.realpath(__file__)), "tao.init")
        self.tao.init("-noplot -lat {lat_path} -init {init_path}".format(lat_path=path_to_lattice, init_path=path_to_init))
        self.ctx = Context.instance()
        self.model_broadcast_socket = zmq.Context().socket(zmq.PUB)
        self.model_broadcast_socket.bind("tcp://*:{}".format(os.environ.get('MODEL_BROADCAST_PORT', 66666)))
        self.loop = asyncio.get_event_loop()
        model_table = NTTable([("element", "s"), ("device_name", "s"),
                                       ("s", "d"), ("length", "d"), ("p0c", "d"),
                                       ("alpha_x", "d"), ("beta_x", "d"), ("eta_x", "d"), ("etap_x", "d"), ("psi_x", "d"),
                                       ("alpha_y", "d"), ("beta_y", "d"), ("eta_y", "d"), ("etap_y", "d"), ("psi_y", "d"),
                                       ("r11", "d"), ("r12", "d"), ("r13", "d"), ("r14", "d"), ("r15", "d"), ("r16", "d"),
                                       ("r21", "d"), ("r22", "d"), ("r23", "d"), ("r24", "d"), ("r25", "d"), ("r26", "d"),
                                       ("r31", "d"), ("r32", "d"), ("r33", "d"), ("r34", "d"), ("r35", "d"), ("r36", "d"),
                                       ("r41", "d"), ("r42", "d"), ("r43", "d"), ("r44", "d"), ("r45", "d"), ("r46", "d"),
                                       ("r51", "d"), ("r52", "d"), ("r53", "d"), ("r54", "d"), ("r55", "d"), ("r56", "d"),
                                       ("r61", "d"), ("r62", "d"), ("r63", "d"), ("r64", "d"), ("r65", "d"), ("r66", "d")])
        initial_table = self.get_twiss_table()
        self.live_twiss_pv = SharedPV(nt=model_table, 
                           initial=initial_table,
                           loop=self.loop)
        self.design_twiss_pv = SharedPV(nt=model_table, 
                           initial=initial_table,
                           loop=self.loop)
        self.pva_needs_refresh = False
        self.need_zmq_broadcast = False
    
    def start(self):
        L.info("Starting Model Service.")
        pva_server = PVAServer(providers=[{"BMAD:SYS0:1:FULL_MACHINE:LIVE:TWISS": self.live_twiss_pv,
                                           "BMAD:SYS0:1:FULL_MACHINE:DESIGN:TWISS": self.design_twiss_pv}])
        zmq_task = self.loop.create_task(self.recv())
        pva_refresh_task = self.loop.create_task(self.refresh_pva_table())
        broadcast_task = self.loop.create_task(self.broadcast_model_changes())
        try:
            self.loop.run_until_complete(zmq_task)
        except KeyboardInterrupt:
            zmq_task.cancel()
            pva_refresh_task.cancel()
            broadcast_task.cancel()
            pva_server.stop()
    
    def get_twiss_table(self):
        start_time = time.time()
        #First we get a list of all the elements.
        element_list = self.tao_cmd("python lat_ele 1@0")
        element_list = [s.split(";") for s in element_list]
        element_id_list, element_name_list = zip(*element_list)
        last_element_index = 0
        for i, row in enumerate(reversed(element_name_list)):
            if row == "END":
                last_element_index = len(element_name_list)-1-i
                break
        element_name_list = element_name_list[1:last_element_index+1]
        element_id_list = element_id_list[1:last_element_index+1]
        s_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.s")
        l_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.l")
        p0c_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.p0c")
        alpha_x_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.a.alpha")
        beta_x_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.a.beta")
        eta_x_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.a.eta")
        etap_x_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.a.etap")
        psi_x_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.a.phi")
        alpha_y_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.b.alpha")
        beta_y_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.b.beta")
        eta_y_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.b.eta")
        etap_y_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.b.etap")
        psi_y_list = self.tao.cmd_real("python lat_list 1@0>>*|model real:ele.b.phi")
        
        table_rows = []
        for i, element_id in enumerate(element_id_list):
            element_name = element_name_list[i]
            try:
                device_name = simulacrum.util.convert_element_to_device(element_name)
            except KeyError:
                device_name = ""
            rmat = _parse_tao_mat6(self.tao.cmd('python ele:mat6 1@0>>{index}|model mat6'.format(index=element_id)))
            if rmat.shape != (6,6):
                rmat = np.empty((6,6))
                rmat.fill(np.nan)
            table_rows.append({"element": element_name, "device_name": device_name, "s": s_list[i], "length": l_list[i], "p0c": p0c_list[i],
                               "alpha_x": alpha_x_list[i], "beta_x": beta_x_list[i], "eta_x": eta_x_list[i], "etap_x": etap_x_list[i], "psi_x": psi_x_list[i],
                               "alpha_y": alpha_y_list[i], "beta_y": beta_y_list[i], "eta_y": eta_y_list[i], "etap_y": etap_y_list[i], "psi_y": psi_y_list[i],
                               "r11": rmat[0,0], "r12": rmat[0,1], "r13": rmat[0,2], "r14": rmat[0,3], "r15": rmat[0,4], "r16": rmat[0,5],
                               "r21": rmat[1,0], "r22": rmat[1,1], "r23": rmat[1,2], "r24": rmat[1,3], "r25": rmat[1,4], "r26": rmat[1,5],
                               "r31": rmat[2,0], "r32": rmat[2,1], "r33": rmat[2,2], "r34": rmat[2,3], "r35": rmat[2,4], "r36": rmat[2,5],
                               "r41": rmat[3,0], "r42": rmat[3,1], "r43": rmat[3,2], "r44": rmat[3,3], "r45": rmat[3,4], "r46": rmat[3,5],
                               "r51": rmat[4,0], "r52": rmat[4,1], "r53": rmat[4,2], "r54": rmat[4,3], "r55": rmat[4,4], "r56": rmat[4,5],
                               "r61": rmat[5,0], "r62": rmat[5,1], "r63": rmat[5,2], "r64": rmat[5,3], "r65": rmat[5,4], "r66": rmat[5,5]})
        end_time = time.time()
        L.debug("get_twiss_table took %f seconds", end_time - start_time)
        return table_rows
    
    async def refresh_pva_table(self):
        """
        This loop continuously checks if the PVAccess table needs to be refreshed,
        and publishes a new table if it does.  The model_has_changed flag is
        usually set when a tao command beginning with 'set' occurs.
        """
        while True:
            if self.pva_needs_refresh:
                self.live_twiss_pv.post(self.get_twiss_table())
                self.pva_needs_refresh = False
            await asyncio.sleep(1.0)
    
    async def broadcast_model_changes(self):
        """
        This loop broadcasts new orbits, twiss parameters, etc. over ZMQ.
        """
        while True:
            if self.need_zmq_broadcast:
                self.send_orbit()
                self.send_profiles_twiss()
                self.send_prof_orbit()
                self.send_und_twiss()
                self.need_zmq_broadcast = False
            await asyncio.sleep(0.1)
    
    def model_changed(self):
        self.pva_needs_refresh = True
        self.need_zmq_broadcast = True
    
    def get_orbit(self):
        start_time = time.time()
        #Get X Orbit
        x_orb_text = self.tao_cmd("show data orbit.x")[3:-2]
        x_orb = _orbit_array_from_text(x_orb_text)
        #Get Y Orbit
        y_orb_text = self.tao_cmd("show data orbit.y")[3:-2]
        y_orb = _orbit_array_from_text(y_orb_text)
        end_time = time.time()
        L.debug("get_orbit took %f seconds", end_time-start_time)
        return np.stack((x_orb, y_orb))

    def get_prof_orbit(self):
        #Get X Orbit
        x_orb_text = self.tao_cmd("show data orbit.profx")[3:-2]
        x_orb = _orbit_array_from_text(x_orb_text)
        #Get Y Orbit
        y_orb_text = self.tao_cmd("show data orbit.profy")[3:-2]
        y_orb = _orbit_array_from_text(y_orb_text)
        return np.stack((x_orb, y_orb))
    
    def get_twiss(self):
        twiss_text = self.tao_cmd("show lat -no_label_lines -at alpha_a -at beta_a -at alpha_b -at beta_b UNDSTART")
        #format to list of comma separated values
        msg='twiss from get_twiss: {}'.format(twiss_text)
        L.info(msg)
        twiss = twiss_text[0].split()
        return twiss

    def old_get_orbit(self):
        #Get X Orbit
        x_orb_text = self.tao_cmd("python lat_list 1@0>>BPM*|model orbit.vec.1")
        x_orb = _orbit_array_from_text(x_orb_text)
        #Get Y Orbit
        y_orb_text = self.tao_cmd("python lat_list 1@0>>BPM*|model orbit.vec.3")
        y_orb = _orbit_array_from_text(y_orb_text)
        return np.stack((x_orb, y_orb))
   
    #information broadcast by the model is sent as two separate messages:
    #metadata message: sent first with 1) tag describing data for services to filter on, 2) type -optional, 3) size -optional
    #data message: sent either as a python object or a series of bits
    
    def send_orbit(self):
        orb = self.get_orbit()
        metadata = {"tag" : "orbit", "dtype": str(orb.dtype), "shape": orb.shape}
        self.model_broadcast_socket.send_pyobj(metadata, zmq.SNDMORE)
        self.model_broadcast_socket.send(orb)

    def send_prof_orbit(self):
        orb = self.get_prof_orbit()
        metadata = {"tag" : "prof_orbit", "dtype": str(orb.dtype), "shape": orb.shape}
        self.model_broadcast_socket.send_pyobj(metadata, zmq.SNDMORE)
        self.model_broadcast_socket.send(orb)

    def send_profiles_twiss(self):
        L.info('Sending Profile');
        twiss_text = np.asarray(self.tao_cmd("show lat -at beta_a -at beta_b Instrument::OTR*,Instrument::YAG*"))
        metadata = {"tag" : "prof_twiss", "dtype": str(twiss_text.dtype), "shape": twiss_text.shape}
        self.model_broadcast_socket.send_pyobj(metadata, zmq.SNDMORE)
        self.model_broadcast_socket.send(np.stack(twiss_text));        
           
    def send_und_twiss(self):
        twiss = self.get_twiss()
        metadata = {"tag": "und_twiss"}
        self.model_broadcast_socket.send_pyobj(metadata, zmq.SNDMORE)
        self.model_broadcast_socket.send_pyobj(twiss)
    
    def tao_cmd(self, cmd):
        if cmd.startswith("exit"):
            return "Please stop trying to exit the model service's Tao, you jerk!"
        result = self.tao.cmd(cmd)
        if cmd.startswith("set"):
            self.model_changed()
        return result
    
    async def recv(self):
        s = self.ctx.socket(zmq.REP)
        s.bind("tcp://*:{}".format(os.environ.get('MODEL_PORT', "12312")))
        while True:
            p = await s.recv_pyobj()
            msg = "Got a message: {}".format(p)
            L.info(msg)
            if p['cmd'] == 'tao':
                try:
                    retval = self.tao_cmd(p['val'])
                    await s.send_pyobj({'status': 'ok', 'result': retval})
                except Exception as e:
                    await s.send_pyobj({'status': 'fail', 'err': e})
            elif p['cmd'] == 'send_orbit':
                self.model_changed() #Sets the flag that will cause an orbit broadcast
                await s.send_pyobj({'status': 'ok'})
            elif p['cmd'] == 'echo':
                    await s.send_pyobj({'status': 'ok', 'result': p['val']})
            elif p['cmd'] == 'send_profiles_twiss':
                self.model_changed() #Sets the flag that will cause a prof broadcast
                #self.send_profiles_twiss()
                #self.send_prof_orbit()
                await s.send_pyobj({'status': 'ok'})
            elif p['cmd'] == 'send_und_twiss':
                self.model_changed() #Sets the flag that will cause an und twiss broadcast
                #self.send_und_twiss()
                await s.send_pyobj({'status': 'ok'})

def _orbit_array_from_text(text):
    return np.array([float(l.split()[5]) for l in text])*1000.0

def _parse_tao_mat6(text):
    return np.array([[float(num) for num in line.split(";")[3:]] for line in text])

if __name__=="__main__":
    serv = ModelService()
    serv.start()

