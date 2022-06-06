# adopt from the domainbed repository

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
from torch.autograd import Variable

import copy
import numpy as np
import random
import modeloperations as mo
import itertools
from collections import defaultdict

import networks
from libs.misc import random_pairs_of_minibatches, ParamDict

ALGORITHMS = [
    'ERM',
]


def get_algorithm_class(algorithm_name):
    """Return the algorithm class with the given name."""
    if algorithm_name not in globals():
        raise NotImplementedError("Algorithm not found: {}".format(algorithm_name))
    return globals()[algorithm_name]


class Algorithm(torch.nn.Module):
    """
    A subclass of Algorithm implements a domain generalization algorithm.
    Subclasses should implement the following:
    - update()
    - predict()
    """
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(Algorithm, self).__init__()
        self.hparams = hparams

    def update(self, minibatches, unlabeled=None):
        """
        Perform one update step, given a list of (x, y) tuples for all
        environments.
        Admits an optional list of unlabeled minibatches from the test domains,
        when task is domain_adaptation.
        """
        raise NotImplementedError

    def predict(self, x, envidx=0):
        raise NotImplementedError

class ERM(Algorithm):
    """
    Empirical Risk Minimization (ERM)
    """
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(ERM, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'])

        self.network = nn.Sequential(self.featurizer, self.classifier)
        self.optimizer = torch.optim.Adam(
            self.network.parameters(),
            lr=self.hparams["lr"],
            weight_decay=self.hparams['weight_decay']
        )

    def update(self, loaders, device='cuda', unlabeled=None):
        objective = 0.
        penalty = 0
        nmb = len(loaders)
        
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index            
            totleni = 0.
            for did, data in enumerate(dataloader):                
                xi, yi = data[0].to(device), data[1].to(device)
                loss = F.cross_entropy(self.predict(xi), yi)

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
        return {'loss': loss.item()}

    def predict(self, x, envidx=0):
        return self.network(x)


class FedAverage(ERM):
    """
    Vanilla Federated Averaging
    """
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(FedAverage, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)
        
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams)
        self.classifier_copy = []
        self.featurizer_copy = []
            
        self.local_center_momentum = 0.
        self.global_center_momentum = 0.
       
        self.feature_center = None
        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]
        else:
            self.local_updates = 10
        
    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        objective = 0.
        penalty = 0
        nmb = len(loaders)
        self.env_feature_centers = [None for i in range(nmb)]
        
        totlen = 0.
        client_len_list = [0 for i in range(nmb)]
        n_envi = len(loaders)
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index
            classifieri = copy.deepcopy(self.classifier)
            featurizersi = copy.deepcopy(self.featurizer)
            optimizer = torch.optim.Adam(
                        list(classifieri.parameters())+list(featurizersi.parameters()) ,
                        lr=self.hparams["lr"],
                        weight_decay=self.hparams['weight_decay'])

            client_len_list[i] = 0.
            local_epochs = self.local_updates


            for id in range(local_epochs):                
                for did, data in enumerate(dataloader):
                    
                    xi, yi = data[0].to(device), data[1].to(device)
                    if id==local_epochs-1:
                        client_len_list[i] += len(xi)
                   
                    featuresi = featurizersi(xi)
                    classifsi = classifieri(featuresi)
                    targetsi = yi

                    if 'regression' in self.hparams.keys():
                        objectivei = nn.MSELoss()(classifsi, targetsi)
                    else:
                        objectivei = F.cross_entropy(classifsi, targetsi)
                    
                    #objectivei = F.cross_entropy(classifsi, targetsi)
                    penalty = 0.

                    optimizer.zero_grad()
                    (objectivei).backward()
                    optimizer.step()
                    if id == local_epochs - 1:
                        objective += objectivei.item()*len(xi)
                        totlen += client_len_list[i]
            self.classifier_copy.append(classifieri)
            self.featurizer_copy.append(featurizersi)

        coefs = torch.ones(n_envi)/n_envi
        self.classifier = mo.scalar_mul(coefs, self.classifier_copy)
        self.featurizer = mo.scalar_mul(coefs, self.featurizer_copy)

        self.classifier_copy = []
        self.featurizer_copy = []
        if torch.is_tensor(penalty):
            penalty = penalty.item()
        objective /= totlen
        return {'loss': objective, 'penalty': penalty}
    def predict(self, x, env=0):
        features = self.featurizer(x) 
        return self.classifier((features))

class SCAFFOLD(ERM):
    """
    Vanilla SCAFFOLD
    """
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(SCAFFOLD, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)
        self.input_shape = input_shape
        self.num_classes = num_classes
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams)

        self.featurizer_c = nn.ModuleList([networks.Featurizer(input_shape, self.hparams) for i in range(hparams['num_client'])])
        self.featurizer_central_c = networks.Featurizer(input_shape, self.hparams)
        mo.zero_weights(self.featurizer_central_c)
        mo.zero_weights(self.featurizer_c)

        self.classifier_c = nn.ModuleList([networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams) for i in range(hparams['num_client'])])
        self.classifier_central_c = networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams)
        mo.zero_weights(self.classifier_central_c)
        mo.zero_weights(self.classifier_c)

        self.classifier_copy = []
        self.featurizer_copy = []
        self.initial = True
              
        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]
        else:
            self.local_updates = 10
        
    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        objective = 0.
        penalty = 0
        nmb = len(loaders)
        
        totlen = 0.
        client_len_list = [0 for i in range(nmb)]
        n_envi = len(loaders)
        learning_rate = 1e-2
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index
            if fedavg:
                #classifieri = copy.deepcopy(self.classifier)
                #featurizersi = copy.deepcopy(self.featurizer)

                classifieri = networks.Classifier(
                            self.featurizer.n_outputs,
                            self.num_classes,
                            self.hparams['nonlinear_classifier'], self.hparams).to(device)
                featurizersi = networks.Featurizer(self.input_shape, self.hparams).to(device)
                classifieri.load_state_dict(self.classifier.state_dict())
                featurizersi.load_state_dict(self.featurizer.state_dict())
                optimizer = torch.optim.SGD(
                        list(classifieri.parameters())+list(featurizersi.parameters()) ,
                        lr=learning_rate,#self.hparams["lr"],
                        weight_decay=self.hparams['weight_decay'])
            else:
                optimizer = torch.optim.SGD(
                        list(self.classifier.parameters())+list(self.featurizer.parameters()) ,
                        lr=self.hparams["lr"],
                        weight_decay=self.hparams['weight_decay'])
            client_len_list[i] = 0.
            if fedavg:
                local_epochs = self.local_updates
            else:
                local_epochs = 1
            cnt = 0.
            for id in range(local_epochs):                
                for did, data in enumerate(dataloader):
                    cnt += 1
                    xi, yi = data[0].to(device), data[1].to(device)
                    if id==local_epochs-1:
                        client_len_list[i] += len(xi)
                   
                    if fedavg:
                        featuresi = featurizersi(xi)
                        classifsi = classifieri(featuresi)
                    else:
                        featuresi = self.featurizer(xi)
                        classifsi = self.classifier(featuresi)#+self.local_classifiers[i](featuresi)
                    targetsi = yi

                    
                    objectivei = F.cross_entropy(classifsi, targetsi)
                    penalty = 0.

                    optimizer.zero_grad()
                    (objectivei).backward()
                    optimizer.step()

                    if False:
                        ciplus = networks.Classifier(
                            self.featurizer.n_outputs,
                            self.num_classes,
                            self.hparams['nonlinear_classifier'], self.hparams).to(device)

                        fiplus = networks.Featurizer(self.input_shape, self.hparams).to(device)
        
                        #ciplus.load_state_dict(classifieri.state_dict())
                        #fiplus = copy.deepcopy(featurizersi)
                        with torch.no_grad():
                            cciparams = list(classifieri.parameters())
                
                            for j, param in enumerate(ciplus.parameters()):
                                param.data = cciparams[j].grad
                                #if j==0:
                                #    print(param.data)
                            fiparams = list(featurizersi.parameters())
                            for j, param in enumerate(fiplus.parameters()):
                                param.data = fiparams[j].grad
                    
                    with torch.no_grad():  
                        clnet_para = classifieri.state_dict()
                        c_local_para = self.classifier_c[i].state_dict()
                        c_global_para = self.classifier_central_c.state_dict()
                        for key in clnet_para:
                            clnet_para[key] = clnet_para[key] -learning_rate * (c_global_para[key] - c_local_para[key])
                        classifieri.load_state_dict(clnet_para)                      
                        
                        net_para = featurizersi.state_dict()
                        c_local_para = self.featurizer_c[i].state_dict()
                        c_global_para = self.featurizer_central_c.state_dict()
                        for key in net_para:
                            net_para[key] = net_para[key] -learning_rate * (c_global_para[key] - c_local_para[key])
                        featurizersi.load_state_dict(net_para)   
            
                    if id == local_epochs - 1:
                        objective += objectivei.item()*len(xi)
                        totlen += client_len_list[i]
            if fedavg:
                self.classifier_copy.append(classifieri)
                self.featurizer_copy.append(featurizersi)

      
            #self.classifier_c[i].load_state_dict(ciplus.state_dict())
            #self.featurizer_c[i].load_state_dict(fiplus.state_dict())
            self.classifier_c[i] = mo.scalar_mul([1,-1, 1/(local_epochs*learning_rate), -1/(cnt*learning_rate)], 
            [self.classifier_c[i], self.classifier_central_c, self.classifier, classifieri])
            self.featurizer_c[i] = mo.scalar_mul([1,-1, 1/(local_epochs*learning_rate), -1/(cnt*learning_rate)], 
            [self.featurizer_c[i], self.featurizer_central_c, self.featurizer, featurizersi])
            
        if fedavg:
            coefs = torch.ones(n_envi)/n_envi
            self.classifier = mo.scalar_mul(coefs, self.classifier_copy)
            self.featurizer = mo.scalar_mul(coefs, self.featurizer_copy)

            self.classifier_central_c = mo.scalar_mul(coefs, self.classifier_c)
            self.featurizer_central_c = mo.scalar_mul(coefs, self.featurizer_c)

            self.classifier_copy = []
            self.featurizer_copy = []

        if torch.is_tensor(penalty):
            penalty = penalty.item()
        objective /= totlen
        self.initial = False
        return {'loss': objective, 'penalty': penalty}

    def predict(self, x, env=0):
        features = self.featurizer(x) 
        return self.classifier((features))

class Feddyn(ERM):
    """
    Vanilla feddyn
    """
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(Feddyn, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams)
        
        self.h_c = networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams)
        self.h_f = networks.Featurizer(input_shape, self.hparams)
        mo.zero_weights(self.h_c)
        mo.zero_weights(self.h_f)

        self.prev_grads_f = nn.ModuleList([networks.Featurizer(input_shape, self.hparams) for i in range(hparams['num_client'])])
        mo.zero_weights(self.prev_grads_f)

        self.prev_grads_c = nn.ModuleList([networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams) for i in range(hparams['num_client'])])
        mo.zero_weights(self.prev_grads_c)
        
        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]
        else:
            self.local_updates = 10
        self.alpha=0.01

        
    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        objective = 0.
        penalty = 0
        nmb = len(loaders)
        self.env_feature_centers = [None for i in range(nmb)]
        
        totlen = 0.
        client_len_list = [0 for i in range(nmb)]
        n_envi = len(loaders)
        classifier_copy = []
        featurizer_copy = []
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index
            classifieri = copy.deepcopy(self.classifier)
            featurizersi = copy.deepcopy(self.featurizer)
            optimizer = torch.optim.Adam(
                        list(classifieri.parameters())+list(featurizersi.parameters()) ,
                        lr=self.hparams["lr"],
                        weight_decay=self.hparams['weight_decay'])

            client_len_list[i] = 0.
            if fedavg:
                local_epochs = self.local_updates
            else:
                local_epochs = 1

            for id in range(local_epochs):                
                for did, data in enumerate(dataloader):
                    
                    xi, yi = data[0].to(device), data[1].to(device)
                    if id==local_epochs-1:
                        client_len_list[i] += len(xi)
                   
                    featuresi = featurizersi(xi)
                    classifsi = classifieri(featuresi)
                    targetsi = yi

                    
                    objectivei = F.cross_entropy(classifsi, targetsi)

                    #linear penalty for featurizer
                    lin_penalty = 0.0                    
                    fidict = self.prev_grads_f[i].state_dict()

                    for name, param in featurizersi.named_parameters():                      
                        lin_penalty += torch.sum(param * fidict[name])
                    objectivei -= lin_penalty

                    #linear penalty for classifier
                    lin_penalty = 0.0
                    cidict = self.prev_grads_c[i].state_dict()
                    for name, param in classifieri.named_parameters():
                        lin_penalty += torch.sum(param * cidict[name])
                    objectivei -= lin_penalty

                    #quadratic penalty for featurizer
                    quad_penalty = 0.0
                    server_featurizer_sd = self.featurizer.state_dict()
                    for name, param in featurizersi.named_parameters():
                        quad_penalty += F.mse_loss(param, server_featurizer_sd[name], reduction='sum')

                    objectivei += self.alpha/2.0 * quad_penalty

                    #quadratic penalty for classifier
                    quad_penalty = 0.0
                    server_classifier_sd = self.classifier.state_dict()
                    for name, param in classifieri.named_parameters():
                        quad_penalty += F.mse_loss(param, server_classifier_sd[name], reduction='sum')

                    objectivei += self.alpha/2.0 * quad_penalty

                    optimizer.zero_grad()
                    (objectivei).backward()
                    optimizer.step()
                    if id == local_epochs - 1:
                        objective += objectivei.item()*len(xi)
                        totlen += client_len_list[i]
            # some operations for each client
            classifier_copy.append(classifieri)
            featurizer_copy.append(featurizersi)
            self.prev_grads_c[i] = mo.scalar_mul([1, self.alpha, -self.alpha], [self.prev_grads_c[i], self.classifier, classifieri])
            self.prev_grads_f[i] = mo.scalar_mul([1, self.alpha, -self.alpha], [self.prev_grads_f[i], self.featurizer, featurizersi])

        # some operations for all clients
        m = nmb
        P = nmb
        coefs1 = [1, self.alpha*P/m] + [-self.alpha/m for i in range(m)]
        coefs2 = [-1/self.alpha] + [1/(0.+P) for i in range(P)]

        self.h_c = mo.scalar_mul(coefs1, [self.h_c, self.classifier]+classifier_copy)        
        self.classifier =  mo.scalar_mul(coefs2, [self.h_c]+classifier_copy)
        
        self.h_f = mo.scalar_mul(coefs1, [self.h_f, self.featurizer]+featurizer_copy)        
        self.featurizer =  mo.scalar_mul(coefs2, [self.h_f]+featurizer_copy)
        
        if torch.is_tensor(lin_penalty):
            lin_penalty = lin_penalty.item()
        objective /= totlen
        return {'loss': objective, 'penalty': lin_penalty}

    def predict(self, x, env=0):
        features = self.featurizer(x) 
        return self.classifier((features))


class TrainThenPersonalize(FedAverage):
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(TrainThenPersonalize, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)
        self.v_list_c = nn.ModuleList([networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams) for i in range(hparams['num_client'])])
        self.v_list_f = nn.ModuleList([networks.Featurizer(input_shape, self.hparams) for i in range(hparams['num_client'])])
        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]
        else:
            self.local_updates = 10
        self.num_round = 0
        self.hparams = hparams

    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        if self.num_round < self.hparams["n_steps"] * 0.95:
            resdict =  super().update(loaders, device, unlabeled, fedavg, id_up)
            for cid in range(len(loaders)):
                self.v_list_c[cid] = copy.deepcopy(self.classifier)
                self.v_list_f[cid] = copy.deepcopy(self.featurizer)
            return resdict
        else:
            objective = 0.
            penalty = 0
            nmb = len(loaders)
            
            totlen = 0.
            client_len_list = [0 for i in range(nmb)]
            n_envi = len(loaders)
            for cid, dataloader in enumerate(loaders):
                #print('client {} started training'.format(cid))
                i = dataloader.index
                local_optim = torch.optim.Adam(
                    list(self.v_list_c[i].parameters())+list(self.v_list_f[i].parameters()) ,
                    lr=self.hparams["lr"],
                    weight_decay=self.hparams['weight_decay']
                )
                

                local_epochs = self.local_updates

                # train the centralized global model
                for id in range(local_epochs):                
                    for did, data in enumerate(dataloader):
                        
                        xi, yi = data[0].to(device), data[1].to(device)
                        
                        if id==local_epochs-1:
                            client_len_list[i] += len(xi)
                        classifsi = self.v_list_c[i](self.v_list_f[i](xi))
                        
                        targetsi = yi

                        objectivei = F.cross_entropy(classifsi, targetsi)
                        penalty = 0.

                        local_optim.zero_grad()
                        (objectivei).backward()
                        local_optim.step()

                        if id == local_epochs - 1:
                            objective += objectivei.item()*len(xi)
                            totlen += client_len_list[i]
                        
            objective /= totlen
        self.num_round += 1
        return {'loss': objective, 'penalty': 0}
    
    def predict(self, x, env=0):
        features = self.v_list_f[env](x) 
        return self.v_list_c[env](features)
 
class CompletelyIndividual(ERM):
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(CompletelyIndividual, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)
        self.v_list_c = nn.ModuleList([networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], self.hparams) for i in range(hparams['num_client'])])
        self.v_list_f = nn.ModuleList([networks.Featurizer(input_shape, self.hparams) for i in range(hparams['num_client'])])
        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]
        else:
            self.local_updates= 10
    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        objective = 0.
        nmb = len(loaders)
        
        totlen = 0.
        client_len_list = [0 for i in range(nmb)]
        n_envi = len(loaders)
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index
            local_optim = torch.optim.Adam(
                list(self.v_list_c[i].parameters())+list(self.v_list_f[i].parameters()) ,
                lr=self.hparams["lr"],
                weight_decay=self.hparams['weight_decay']
            )

            local_epochs = self.local_updates


            # train the centralized global model
            for id in range(local_epochs):                
                for did, data in enumerate(dataloader):
                    
                    xi, yi = data[0].to(device), data[1].to(device)
                    
                    if id==local_epochs-1:
                        client_len_list[i] += len(xi)
                    classifsi = self.v_list_c[i](self.v_list_f[i](xi))
                    
                    targetsi = yi
                    
                    if 'regression' in self.hparams.keys():
                        objectivei = nn.MSELoss()(classifsi, targetsi)
                    else:
                        objectivei = F.cross_entropy(classifsi, targetsi) 
                    penalty = 0.

                    local_optim.zero_grad()
                    (objectivei).backward()
                    local_optim.step()

                    if id == local_epochs - 1:
                        objective += objectivei.item()*len(xi)
                        totlen += client_len_list[i]
                    
        objective /= totlen
        return {'loss': objective, 'penalty': 0}
    
    def predict(self, x, env=0):
        features = self.v_list_f[env](x) 
        return self.v_list_c[env]((features))

class Ditto(FedAverage):
    '''
    an implementation of ditto
    '''
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(Ditto, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)
        self.v_list_c = nn.ModuleList([networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], self.hparams) for i in range(hparams['num_client'])])
        self.v_list_f = nn.ModuleList([networks.Featurizer(input_shape, self.hparams) for i in range(hparams['num_client'])])
        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]

    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        objective = 0.
        penalty = 0
        nmb = len(loaders)
        self.env_feature_centers = [None for i in range(nmb)]
        
        totlen = 0.
        client_len_list = [0 for i in range(nmb)]
        n_envi = len(loaders)
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index
            local_optim = torch.optim.Adam(
                list(self.v_list_c[i].parameters())+list(self.v_list_f[i].parameters()) ,
                lr=self.hparams["lr"],
                weight_decay=self.hparams['weight_decay']
            )
            classifieri = copy.deepcopy(self.classifier)
            featurizersi = copy.deepcopy(self.featurizer)
            optimizer = torch.optim.Adam(
                        list(classifieri.parameters())+list(featurizersi.parameters()) ,
                        lr=self.hparams["lr"],
                        weight_decay=self.hparams['weight_decay'])

            client_len_list[i] = 0.
            local_epochs = self.local_updates


            # train the centralized global model
            for id in range(local_epochs):                
                for did, data in enumerate(dataloader):
                    
                    xi, yi = data[0].to(device), data[1].to(device)
                    if id==local_epochs-1:
                        client_len_list[i] += len(xi)
                    featuresi = featurizersi(xi)
                    classifsi = classifieri(featuresi)

                    targetsi = yi

                    objectivei = F.cross_entropy(classifsi, targetsi)
                    penalty = 0.

                    optimizer.zero_grad()
                    objectivei.backward()
                    optimizer.step()

                    if id == local_epochs - 1:
                        objective += objectivei.item()*len(xi)
                        totlen += client_len_list[i]
                    
            # train localized models
            for id in range(local_epochs):                
                for did, data in enumerate(dataloader):
                    xi, yi = data[0].to(device), data[1].to(device)
                    if id==local_epochs-1:
                        client_len_list[i] += len(xi)

                    featuresi = self.v_list_f[i](xi)
                    classifsi = self.v_list_c[i](featuresi)
                     
                    targetsi = yi
                    
                    objectivei = F.cross_entropy(classifsi, targetsi)
                    
                    local_optim.zero_grad()
                    (objectivei).backward()
                    local_optim.step()

                    # move toward the central model
                    beta=0.99
                    self.v_list_c[i] = mo.scalar_mul([beta, 1-beta],[self.v_list_c[i], self.classifier])
                    self.v_list_f[i] = mo.scalar_mul([beta, 1-beta],[self.v_list_f[i], self.featurizer])

                    
            self.classifier_copy.append(classifieri)
            self.featurizer_copy.append(featurizersi)

        coefs = torch.ones(n_envi)/n_envi
        self.classifier = mo.scalar_mul(coefs, self.classifier_copy)
        self.featurizer = mo.scalar_mul(coefs, self.featurizer_copy)

        self.classifier_copy = []
        self.featurizer_copy = []
        if torch.is_tensor(penalty):
            penalty = penalty.item()
        objective /= totlen

        return {'loss': objective, 'penalty': penalty}
    
    def predict(self, x, env=0):
        features = self.v_list_f[env](x) 
        return self.v_list_c[env]((features))
    
class SimpleDA(ERM):
    """
    Implementation of simple domain adaptation
    """
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(SimpleDA, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)

        self.featurizers = nn.ModuleList([networks.Featurizer(input_shape, self.hparams) for i in range(hparams['num_client'])])
        self.classifier = networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams)
        self.classifier_copy = []

        self.local_center_momentum = 0.
        self.global_center_momentum = 0.

        self.feature_center = None
        self.env_feature_centers = []
        self.val_env_feature_centers = [None for i in range(hparams['num_client'])]

        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]
        else:
            self.local_updates = 10

    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        objective = 0.
        penalty = 0
        nmb = len(loaders)
        self.env_feature_centers = [None for i in range(nmb)]

        totlen = 0.
        client_len_list = [0 for i in range(nmb)]
        n_envi = len(loaders)
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index

            classifieri = copy.deepcopy(self.classifier)
            optimizer = torch.optim.Adam(
                        list(classifieri.parameters())+list(self.featurizers[i].parameters()) ,
                        lr=self.hparams["lr"],
                        weight_decay=self.hparams['weight_decay'])

            client_len_list[i] = 0.
            local_epochs = self.local_updates

            for id in range(local_epochs):
                for did, data in enumerate(dataloader):
                    xi, yi = data[0].to(device), data[1].to(device)
                    if id==local_epochs-1:
                        client_len_list[i] += len(xi)

                    featuresi = self.featurizers[i](xi)
                    classifsi = classifieri(featuresi)
                    targetsi = yi

                    objectivei = F.cross_entropy(classifsi, targetsi)

                    penalty = 0.

                    optimizer.zero_grad()
                    objectivei.backward()
                    optimizer.step()
                    if id == local_epochs - 1:
                        objective += objectivei.item()*len(xi)
                        totlen += client_len_list[i]
            self.classifier_copy.append(classifieri)

        coefs = torch.ones(n_envi)/n_envi
        self.classifier = mo.scalar_mul(coefs, self.classifier_copy)
        self.classifier_copy = []

        if torch.is_tensor(penalty):
            penalty = penalty.item()
        objective /= totlen

        return {'loss': objective, 'penalty': penalty}

    def predict(self, x, env=0):
        features = self.featurizers[env](x)
        return self.classifier((features))


class Pfedme(ERM):
    """
    an implementation of pfedme with adam
    """
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(Pfedme, self).__init__(input_shape, num_classes, num_domains,
                                  hparams)
        self.featurizer = networks.Featurizer(input_shape, self.hparams)
        self.classifier = networks.Classifier(
            self.featurizer.n_outputs,
            num_classes,
            self.hparams['nonlinear_classifier'], hparams)

        
        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]
        else:
            self.local_updates = 10
        #self.local_updates = 1
        self.lbd= 0.05#15
        self.eta= 5#0.005
        print("regularizing lambda {}".format(self.lbd))
        print("stepsize eta {}".format(self.eta))


        
    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        objective = 0.
        penalty = 0
        nmb = len(loaders)
        self.env_feature_centers = [None for i in range(nmb)]
        
        totlen = 0.
        client_len_list = [0 for i in range(nmb)]
        n_envi = len(loaders)
        classifier_copy = []
        featurizer_copy = []
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index
            if fedavg:
                classifieri = copy.deepcopy(self.classifier)
                featurizersi = copy.deepcopy(self.featurizer)
                classifierislow = copy.deepcopy(self.classifier)  
                featurizerislow = copy.deepcopy(self.featurizer)
                optimizer = torch.optim.Adam(
                        list(classifieri.parameters())+list(featurizersi.parameters()) ,
                        lr=self.hparams["lr"],
                        weight_decay=self.hparams['weight_decay'])
            
            client_len_list[i] = 0.
            if fedavg:
                local_epochs = self.local_updates
            R = 0
            for id in range(local_epochs): 
                for did, data in enumerate(dataloader):
                    R += 1
                    K = 3
                    for j in range(K):
                        xi, yi = data[0].to(device), data[1].to(device)
                        if id==local_epochs-1:
                            client_len_list[i] += len(xi)
                    
                        if fedavg:
                            featuresi = featurizersi(xi)
                            classifsi = classifieri(featuresi)
                        targetsi = yi
                        
                        objectivei = F.cross_entropy(classifsi, targetsi)

                        #quadratic penalty for featurizer
                        quad_penalty = 0.0
                        server_featurizer_sd = featurizerislow.state_dict()
                        for name, param in featurizersi.named_parameters():
                            quad_penalty += F.mse_loss(param, server_featurizer_sd[name], reduction='sum')

                        objectivei += self.lbd/2.0 * quad_penalty
                        #quadratic penalty for classifier
                        quad_penalty = 0.0
                        server_classifier_sd = classifierislow.state_dict()
                        for name, param in classifieri.named_parameters():
                            quad_penalty += F.mse_loss(param, server_classifier_sd[name], reduction='sum')

                        objectivei += self.lbd/2.0 * quad_penalty

                        optimizer.zero_grad()
                        (objectivei).backward()
                        optimizer.step()

                    # after K inner inner iterations, update slow parameters
                    classifierislow = mo.scalar_mul([1-self.eta*self.lbd, self.eta*self.lbd],[classifierislow, classifieri])
                    featurizerislow = mo.scalar_mul([1-self.eta*self.lbd, self.eta*self.lbd],[featurizerislow, featurizersi])

                if id == local_epochs - 1:
                    objective += objectivei.item()*len(xi)
                    totlen += len(xi)
            # some operations for each client
            classifier_copy.append(classifierislow)
            featurizer_copy.append(featurizerislow)
        # some operations for all clients
        # nmb is the number of clients 
        coefs1 = [1/nmb for i in range(nmb)]

        self.classifier =  mo.scalar_mul(coefs1, classifier_copy)
        self.featurizer =  mo.scalar_mul(coefs1, featurizer_copy)
        
        objective /= totlen
        #print(R)
        return {'loss': objective, 'penalty': 0}

    def predict(self, x, env=0):
        features = self.featurizer(x) 
        return self.classifier((features))

class PFLDA(FeatureNormalize):
    def __init__(self, input_shape, num_classes, num_domains, hparams, gaussian=True):
        super(PFLDA, self).__init__(input_shape, num_classes, num_domains, hparams, gaussian)

        self.global_featurizer = networks.Featurizer(input_shape, self.hparams)
        self.featurizers = nn.ModuleList([copy.deepcopy(self.global_featurizer) for i in range(hparams['num_client'])])

        self.local_classifiers = nn.ModuleList([copy.deepcopy(self.classifier) for i in range(hparams['num_client']) ])

        self.local_decay  =1-1e-2
        self.lr_decay = 0.99
        if "local_updates" in hparams.keys():
            self.local_updates = hparams["local_updates"]

        self.featurizer_copy = []


    def update(self, loaders, device='cuda', unlabeled=None, fedavg=True, id_up=0):
        objective = 0.
        penalty = 0
        nmb = len(loaders)
        #self.env_feature_centers = [None for i in range(nmb)]
        totlen = 0.
        client_len_list = [0 for i in range(nmb)]
        n_envi = len(loaders)
        for cid, dataloader in enumerate(loaders):
            #print('client {} started training'.format(cid))
            i = dataloader.index
            classifieri = copy.deepcopy(self.classifier)
            featurizeri = copy.deepcopy(self.global_featurizer)
            optimizer_g = torch.optim.Adam(
                        list(classifieri.parameters())+list(featurizeri.parameters())+list(self.featurizers[i].parameters()) ,
                        lr=self.hparams["lr"]*self.lr_decay**(1+id_up//10),
                        weight_decay=self.hparams['weight_decay'])

            optimizer_l = torch.optim.Adam(
                    self.local_classifiers[i].parameters(),
                    lr=self.hparams["lr"]*self.lr_decay**(1+id_up//10),
                    weight_decay=self.hparams['weight_decay'])
            client_len_list[i] = 0.
            
            local_epochs = self.local_updates

            for id in range(local_epochs):
                for did, data in enumerate(dataloader):
                    xi, yi = data[0].to(device), data[1].to(device)

                    client_len_list[i] += len(xi)
                    featuresi = self.featurizers[i](xi)
                    classifsi = classifieri(featuresi)
                    targetsi = yi
                    objectivei = F.cross_entropy(classifsi, targetsi)
                    penalty = 0.

                    optimizer_g.zero_grad()
                    optimizer_l.zero_grad()
                    objectivei.backward()
                    optimizer_g.step()

                    # local_update_regularized
                    #optimizer_l.zero_grad()
                    xi, yi = data[0].to(device), data[1].to(device)
                    with torch.no_grad():
                        featuresi = self.global_featurizer(xi)
                    yi_l = self.local_classifiers[i](featuresi)
                    loss1 = F.cross_entropy(yi_l, targetsi)
                    loss1.backward()
                    optimizer_l.step()
                    beta3 = 0.99
                    self.local_classifiers[i] = mo.scalar_mul([beta3, 1-beta3], [self.classifier, self.local_classifiers[i]])

                    objective += objectivei.item()*len(xi)

                    if id == local_epochs - 1:
                        objective += objectivei.item()*len(xi)
                        totlen += client_len_list[i]
            self.classifier_copy.append(classifieri)
            self.featurizer_copy.append(featurizeri)

        coefs = torch.ones(n_envi)/n_envi
        self.classifier = mo.scalar_mul(coefs, self.classifier_copy)
        self.global_featurizer = mo.scalar_mul(coefs, self.featurizer_copy)
        self.classifier_copy = []
        self.featurizer_copy = []

        if torch.is_tensor(penalty):
            penalty = penalty.item()
        objective /= totlen
        return {'loss': objective, 'penalty': penalty}

    def predict(self, x, env=0):
        features = self.featurizers[env](x)
        return self.local_classifiers[env](features)
